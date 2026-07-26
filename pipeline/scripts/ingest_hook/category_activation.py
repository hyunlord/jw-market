"""Production/shadow activation for verified ingest category table manifests."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final


ENV_APPROVED: Final = "INGEST_CATEGORY_ACTIVATION_APPROVED"
ENV_BUILD_PREFIX: Final = "INGEST_CATEGORY_ACTIVATION_BUILD_PREFIX"
ENV_TARGET_IQVIA_NSA_DB: Final = "INGEST_CATEGORY_TARGET_IQVIA_NSA_DB"
ENV_TARGET_CSD_RAW_DB: Final = "INGEST_CATEGORY_TARGET_CSD_RAW_DB"
ENV_TARGET_CSD_STAGE_DB: Final = "INGEST_CATEGORY_TARGET_CSD_STAGE_DB"
ENV_TARGET_MI_MASTER_DB: Final = "INGEST_CATEGORY_TARGET_MI_MASTER_DB"

SHADOW_SCHEMA_PREFIX: Final = "jw_ingest_shadow_"
_STAGING_SCHEMA_RE: Final = re.compile(r"^jw_ingest_[A-Za-z0-9_]+$")
_SCHEMA_RE: Final = re.compile(r"^[A-Za-z0-9_]+$")
_RAW_TABLES: Final = frozenset(
    {"raw_csd_channel_dynamics", "raw_keyword_events"}
)
_KEYWORD_STAGE_COLUMNS: Final = (
    "period_ym", "visit_location", "specialty", "representing_company",
    "product_name", "therapeutic_class", "keyword_text", "interest",
    "prescription_frequency", "prescription_evolution", "abstract_lit",
    "patient_lit", "promotional_lit", "samples_left", "other_materials_left",
    "what_other_materials", "other_comments", "source_file", "source_sheet",
    "source_row_no", "source_file_sha256", "stage_row_sha256", "loaded_at",
)


@dataclass(frozen=True, slots=True)
class TableEvidence:
    schema: str
    table: str


@dataclass(frozen=True, slots=True)
class TableTarget:
    table: str
    target_schema: str


@dataclass(frozen=True, slots=True)
class CategoryActivationSpec:
    tables: tuple[str, ...]
    target_env: tuple[str, ...]
    nsa_window: bool = False


@dataclass(frozen=True, slots=True)
class ActivationPlan:
    category: str
    epoch: str
    run_id: str
    build_schema: str
    tables: tuple[TableEvidence, ...]
    targets: tuple[TableTarget, ...]
    nsa_quarters: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ActivationResult:
    category: str
    epoch: str
    run_id: str
    build_schema: str
    tables: tuple[str, ...]
    target_tables: tuple[str, ...]
    row_counts: dict[str, int]
    dry_run: bool
    published: bool
    bootstrapped_tables: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RestorePlan:
    result: ActivationResult
    targets: tuple[TableTarget, ...]


class ActivationError(RuntimeError):
    """Raised when category activation would violate a fail-closed contract."""


_SPECS: Final[dict[str, CategoryActivationSpec]] = {
    "iqvia_nsa": CategoryActivationSpec(
        ("iqvia_nsa_quarterly_raw",), (ENV_TARGET_IQVIA_NSA_DB,), True
    ),
    "iqvia_csd_channel": CategoryActivationSpec(
        ("raw_csd_channel_dynamics", "csd_channel_dynamics_stage"),
        (ENV_TARGET_CSD_RAW_DB, ENV_TARGET_CSD_STAGE_DB),
    ),
    "iqvia_csd_keyword": CategoryActivationSpec(
        ("raw_keyword_events", "km_keyword_event_stage"),
        (ENV_TARGET_CSD_RAW_DB, ENV_TARGET_CSD_STAGE_DB),
    ),
    "mi_master": CategoryActivationSpec(
        ("stg_master_market_definition", "stg_master_mapping_table"),
        (ENV_TARGET_MI_MASTER_DB, ENV_TARGET_MI_MASTER_DB),
    ),
}


def supports(category: str) -> bool:
    """Return true when the category has a bounded activation contract."""
    return category in _SPECS


def activate(
    category: str,
    staging_manifest: Path | dict[str, Any],
    epoch: str,
    run_id: str,
    dry_run: bool = False,
) -> ActivationResult:
    """Promote a verified category table manifest through candidate and swap gates."""
    plan = prepare(category, staging_manifest, epoch, run_id)
    if dry_run:
        return _result(plan, row_counts={}, dry_run=True, published=False)
    connection = connect()
    try:
        row_counts, bootstrapped_tables = load(connection, plan)
        publish(connection, plan, bootstrapped_tables)
        connection.commit()
        return _result(
            plan,
            row_counts=row_counts,
            dry_run=False,
            published=True,
            bootstrapped_tables=bootstrapped_tables,
        )
    except Exception:
        rollback = getattr(connection, "rollback", None)
        if callable(rollback):
            rollback()
        raise
    finally:
        close = getattr(connection, "close", None)
        if callable(close):
            close()


def restore(result: ActivationResult) -> None:
    """Restore target tables from the backups created by a successful activation."""
    plan = _restore_plan(result)
    connection = connect()
    try:
        restore_publish(connection, plan)
        connection.commit()
    except Exception:
        rollback = getattr(connection, "rollback", None)
        if callable(rollback):
            rollback()
        raise
    finally:
        close = getattr(connection, "close", None)
        if callable(close):
            close()


def finalize(result: ActivationResult) -> None:
    """Remove rollback artifacts after the ingest run is durably complete."""
    plan = _restore_plan(result)
    connection = connect()
    try:
        with connection.cursor() as cursor:
            for target in plan.targets:
                target_name = f"{target.target_schema}.{target.table}"
                if target_name in plan.result.bootstrapped_tables:
                    continue
                old_table = _old_table_name(target.table, result.run_id)
                cursor.execute(
                    "DROP TABLE IF EXISTS "
                    f"`{target.target_schema}`.`{old_table}`"
                )
            cursor.execute(f"DROP SCHEMA IF EXISTS `{result.build_schema}`")
        connection.commit()
    except Exception:
        rollback = getattr(connection, "rollback", None)
        if callable(rollback):
            rollback()
        raise
    finally:
        close = getattr(connection, "close", None)
        if callable(close):
            close()


def prepare(
    category: str,
    staging_manifest: Path | dict[str, Any],
    epoch: str,
    run_id: str,
) -> ActivationPlan:
    """Validate manifest/schema targets and return a deterministic activation plan."""
    spec = _spec_for(category)
    manifest = _read_manifest(staging_manifest)
    if str(manifest.get("category")) != category:
        raise ActivationError(
            f"manifest category mismatch: expected={category!r} actual={manifest.get('category')!r}"
        )
    if str(manifest.get("epoch")) != epoch:
        raise ActivationError(
            f"manifest epoch mismatch: expected={epoch!r} actual={manifest.get('epoch')!r}"
        )
    targets = _targets(spec)
    evidence = _manifest_tables(manifest, spec)
    build_schema = _build_schema(run_id)
    nsa_quarters = _nsa_window(epoch) if spec.nsa_window else ()
    return ActivationPlan(
        category=category,
        epoch=epoch,
        run_id=_safe_run_id(run_id),
        build_schema=build_schema,
        tables=evidence,
        targets=targets,
        nsa_quarters=nsa_quarters,
    )


def load(connection: Any, plan: ActivationPlan) -> tuple[dict[str, int], tuple[str, ...]]:
    """Build and validate candidate tables from verified staging tables."""
    row_counts: dict[str, int] = {}
    bootstrapped_tables: list[str] = []
    with connection.cursor() as cursor:
        cursor.execute(f"CREATE SCHEMA IF NOT EXISTS `{plan.build_schema}`")
        for evidence, target in zip(plan.tables, plan.targets, strict=True):
            target_exists = bool(
                _writable_columns(
                    cursor,
                    target.target_schema,
                    target.table,
                    required=False,
                )
            )
            if not target_exists:
                bootstrapped_tables.append(f"{target.target_schema}.{target.table}")
            cursor.execute(
                f"DROP TABLE IF EXISTS `{plan.build_schema}`.`{evidence.table}`"
            )
            cursor.execute(
                f"CREATE TABLE `{plan.build_schema}`.`{evidence.table}` "
                f"LIKE `{evidence.schema}`.`{evidence.table}`"
            )
            _insert_candidate_rows(
                cursor,
                plan,
                evidence,
                target,
                target_exists=target_exists,
            )
            cursor.execute(
                f"SELECT COUNT(*) FROM `{plan.build_schema}`.`{evidence.table}`"
            )
            rows = int(cursor.fetchone()[0])
            if rows < 1:
                raise ActivationError(
                    f"candidate table is empty: {plan.build_schema}.{evidence.table}"
                )
            row_counts[evidence.table] = rows
    return row_counts, tuple(bootstrapped_tables)


def publish(
    connection: Any,
    plan: ActivationPlan,
    bootstrapped_tables: tuple[str, ...] = (),
) -> None:
    """Swap every candidate table into its target schema with one RENAME TABLE."""
    moves: list[str] = []
    bootstrap = set(bootstrapped_tables)
    for target in plan.targets:
        target_name = f"{target.target_schema}.{target.table}"
        if target_name not in bootstrap:
            backup = f"{target.table}__old_{plan.run_id}"
            moves.append(
                f"`{target.target_schema}`.`{target.table}` "
                f"TO `{target.target_schema}`.`{backup}`"
            )
        moves.append(
            f"`{plan.build_schema}`.`{target.table}` "
            f"TO `{target.target_schema}`.`{target.table}`"
        )
    with connection.cursor() as cursor:
        cursor.execute("RENAME TABLE " + ", ".join(moves))


def restore_publish(connection: Any, plan: RestorePlan) -> None:
    """Atomically swap backup tables back into target names and clean artifacts."""
    with connection.cursor() as cursor:
        _assert_restore_ready(cursor, plan)
        moves: list[str] = []
        failed_tables: list[str] = []
        for target in plan.targets:
            old_table = _old_table_name(target.table, plan.result.run_id)
            failed_table = _failed_table_name(target.table, plan.result.run_id)
            target_name = f"{target.target_schema}.{target.table}"
            moves.append(
                f"`{target.target_schema}`.`{target.table}` "
                f"TO `{target.target_schema}`.`{failed_table}`"
            )
            if target_name not in plan.result.bootstrapped_tables:
                moves.append(
                    f"`{target.target_schema}`.`{old_table}` "
                    f"TO `{target.target_schema}`.`{target.table}`"
                )
            failed_tables.append(
                f"`{target.target_schema}`.`{failed_table}`"
            )
        cursor.execute("RENAME TABLE " + ", ".join(moves))
        for failed_table in failed_tables:
            cursor.execute(f"DROP TABLE IF EXISTS {failed_table}")
        cursor.execute(f"DROP SCHEMA IF EXISTS `{plan.result.build_schema}`")


def connect() -> Any:
    """Open the configured MariaDB connection without selecting a target schema."""
    import pymysql

    return pymysql.connect(
        host=os.environ.get("MARIADB_HOST", "127.0.0.1"),
        port=int(os.environ.get("MARIADB_PORT", "3306")),
        user=os.environ.get("MARIADB_USER", ""),
        password=os.environ.get("MARIADB_PASSWORD", ""),
        charset="utf8mb4",
        autocommit=False,
    )


def _result(
    plan: ActivationPlan,
    *,
    row_counts: dict[str, int],
    dry_run: bool,
    published: bool,
    bootstrapped_tables: tuple[str, ...] = (),
) -> ActivationResult:
    return ActivationResult(
        category=plan.category,
        epoch=plan.epoch,
        run_id=plan.run_id,
        build_schema=plan.build_schema,
        tables=tuple(evidence.table for evidence in plan.tables),
        target_tables=tuple(
            f"{target.target_schema}.{target.table}" for target in plan.targets
        ),
        row_counts=row_counts,
        dry_run=dry_run,
        published=published,
        bootstrapped_tables=bootstrapped_tables,
    )


def _restore_plan(result: ActivationResult) -> RestorePlan:
    if not result.published:
        raise ActivationError("restore requires a published activation result")
    if len(result.tables) != len(result.target_tables):
        raise ActivationError("activation result target/table arity mismatch")
    targets: list[TableTarget] = []
    for table, target_table in zip(result.tables, result.target_tables, strict=True):
        schema, parsed_table = _split_target_table(target_table)
        if parsed_table != table:
            raise ActivationError(
                f"activation result target identity mismatch: {target_table!r}"
            )
        targets.append(TableTarget(table=table, target_schema=schema))
    _activation_schema(result.build_schema, "activation result build_schema")
    return RestorePlan(result=result, targets=tuple(targets))


def _split_target_table(target_table: str) -> tuple[str, str]:
    pieces = target_table.split(".", 1)
    if len(pieces) != 2:
        raise ActivationError(
            f"activation result target table must be schema.table: {target_table!r}"
        )
    schema, table = pieces
    return (
        _activation_schema(schema, "activation result target schema"),
        _safe_identifier(table, "activation result target table"),
    )


def _spec_for(category: str) -> CategoryActivationSpec:
    spec = _SPECS.get(category)
    if spec is None:
        raise ActivationError(f"unsupported category activation: {category!r}")
    return spec


def _read_manifest(source: Path | dict[str, Any]) -> dict[str, Any]:
    if isinstance(source, Path):
        manifest_path = source / "_manifest.json" if source.is_dir() else source
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ActivationError(f"staging manifest unreadable: {manifest_path}") from exc
    else:
        loaded = source
    if not isinstance(loaded, dict):
        raise ActivationError("staging manifest must be a JSON object")
    if loaded.get("schema_version") != "ingest-table-load-v1":
        raise ActivationError(
            f"unsupported staging manifest schema: {loaded.get('schema_version')!r}"
        )
    return loaded


def _manifest_tables(
    manifest: dict[str, Any], spec: CategoryActivationSpec
) -> tuple[TableEvidence, ...]:
    raw_tables = manifest.get("tables")
    if not isinstance(raw_tables, list):
        raise ActivationError("staging manifest tables must be a list")
    by_table: dict[str, TableEvidence] = {}
    for raw in raw_tables:
        if not isinstance(raw, dict):
            raise ActivationError("staging manifest table entry must be an object")
        table = str(raw.get("table") or "")
        if table not in spec.tables:
            raise ActivationError(f"table {table!r} is outside category allowlist")
        schema = _staging_schema(str(raw.get("schema") or ""))
        by_table[table] = TableEvidence(schema=schema, table=table)
    missing = [table for table in spec.tables if table not in by_table]
    if missing:
        raise ActivationError(f"staging manifest missing required table(s): {missing}")
    return tuple(by_table[table] for table in spec.tables)


def _staging_schema(schema: str) -> str:
    if _STAGING_SCHEMA_RE.fullmatch(schema) is None:
        raise ActivationError(f"refusing non-ingest staging schema: {schema!r}")
    return schema


def _build_schema(run_id: str) -> str:
    prefix = os.environ.get(ENV_BUILD_PREFIX, "").strip()
    if not prefix:
        raise ActivationError(f"{ENV_BUILD_PREFIX} is required")
    schema = f"{prefix}_{_safe_run_id(run_id)}"
    _activation_schema(schema, ENV_BUILD_PREFIX)
    return schema


def _targets(spec: CategoryActivationSpec) -> tuple[TableTarget, ...]:
    targets: list[TableTarget] = []
    for table, env_name in zip(spec.tables, spec.target_env, strict=True):
        schema = os.environ.get(env_name, "").strip()
        if not schema:
            raise ActivationError(f"{env_name} is required")
        targets.append(TableTarget(table=table, target_schema=_activation_schema(schema, env_name)))
    return tuple(targets)


def _activation_schema(schema: str, label: str) -> str:
    if _SCHEMA_RE.fullmatch(schema) is None:
        raise ActivationError(f"{label} is not a safe schema identifier: {schema!r}")
    if schema.startswith(SHADOW_SCHEMA_PREFIX):
        return schema
    if os.environ.get(ENV_APPROVED, "").strip() != "1":
        raise ActivationError(
            f"{label}={schema!r} requires {ENV_APPROVED}=1 unless it is an isolated shadow schema"
        )
    return schema


def _safe_identifier(value: str, label: str) -> str:
    if _SCHEMA_RE.fullmatch(value) is None:
        raise ActivationError(f"{label} is not a safe identifier: {value!r}")
    return value


def _assert_restore_ready(cursor: Any, plan: RestorePlan) -> None:
    for target in plan.targets:
        old_table = _old_table_name(target.table, plan.result.run_id)
        if not _table_exists(cursor, target.target_schema, target.table):
            raise ActivationError(
                f"rollback target missing: {target.target_schema}.{target.table}"
            )
        target_name = f"{target.target_schema}.{target.table}"
        if target_name in plan.result.bootstrapped_tables:
            continue
        if not _table_exists(cursor, target.target_schema, old_table):
            raise ActivationError(
                f"rollback backup missing: {target.target_schema}.{old_table}"
            )
        if not _tables_have_same_identity(
            cursor, target.target_schema, target.table, old_table
        ):
            raise ActivationError(
                f"rollback backup identity mismatch: {target.target_schema}.{old_table}"
            )


def _table_exists(cursor: Any, schema: str, table: str) -> bool:
    cursor.execute(
        "SELECT COUNT(*) FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
        (schema, table),
    )
    return int(cursor.fetchone()[0]) == 1


def _tables_have_same_identity(
    cursor: Any, schema: str, target_table: str, backup_table: str
) -> bool:
    cursor.execute(
        "SELECT COUNT(*) FROM ("
        "SELECT ORDINAL_POSITION, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, "
        "COLUMN_KEY, COLUMN_DEFAULT, EXTRA "
        "FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
        "UNION ALL "
        "SELECT ORDINAL_POSITION, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, "
        "COLUMN_KEY, COLUMN_DEFAULT, EXTRA "
        "FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s"
        ") identity_check "
        "GROUP BY ORDINAL_POSITION, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, "
        "COLUMN_KEY, COLUMN_DEFAULT, EXTRA "
        "HAVING COUNT(*) = 1",
        (schema, target_table, schema, backup_table),
    )
    return int(cursor.fetchone()[0]) == 0


def _old_table_name(table: str, run_id: str) -> str:
    return f"{table}__old_{_safe_run_id(run_id)}"


def _failed_table_name(table: str, run_id: str) -> str:
    return f"{table}__failed_{_safe_run_id(run_id)}"


def _insert_candidate_rows(
    cursor: Any,
    plan: ActivationPlan,
    evidence: TableEvidence,
    target: TableTarget,
    *,
    target_exists: bool,
) -> None:
    match evidence.table:
        case _ if evidence.table in _RAW_TABLES:
            _insert_append_candidate(
                cursor, plan, evidence, target, target_exists=target_exists
            )
        case "iqvia_nsa_quarterly_raw":
            _insert_period_candidate(
                cursor,
                plan,
                evidence,
                target,
                period_column="period_label",
                allowed_periods=plan.nsa_quarters,
                target_exists=target_exists,
            )
        case "csd_channel_dynamics_stage":
            _insert_period_candidate(
                cursor,
                plan,
                evidence,
                target,
                period_column="period_ym",
                target_exists=target_exists,
            )
        case "km_keyword_event_stage":
            _insert_keyword_stage_candidate(
                cursor,
                plan,
                evidence,
                target,
                target_exists=target_exists,
            )
        case "stg_master_market_definition" | "stg_master_mapping_table":
            cursor.execute(
                f"INSERT INTO `{plan.build_schema}`.`{evidence.table}` "
                f"SELECT * FROM `{evidence.schema}`.`{evidence.table}`"
            )
        case unreachable:
            raise ActivationError(
                f"candidate strategy is undefined for table {unreachable!r}"
            )


def _insert_append_candidate(
    cursor: Any,
    plan: ActivationPlan,
    evidence: TableEvidence,
    target: TableTarget,
    *,
    target_exists: bool,
) -> None:
    candidate = f"`{plan.build_schema}`.`{evidence.table}`"
    columns, existing_columns, staged_columns = _copy_columns(
        cursor,
        evidence,
        target,
        target_exists=target_exists,
    )
    if target_exists:
        cursor.execute(
            f"INSERT INTO {candidate} ({columns}) "
            f"SELECT {existing_columns} "
            f"FROM `{target.target_schema}`.`{target.table}` existing"
        )
    cursor.execute(
        f"INSERT IGNORE INTO {candidate} ({columns}) "
        f"SELECT {staged_columns} "
        f"FROM `{evidence.schema}`.`{evidence.table}` staged"
    )


def _insert_period_candidate(
    cursor: Any,
    plan: ActivationPlan,
    evidence: TableEvidence,
    target: TableTarget,
    *,
    period_column: str,
    allowed_periods: tuple[str, ...] = (),
    target_exists: bool,
) -> None:
    candidate = f"`{plan.build_schema}`.`{evidence.table}`"
    staged = f"`{evidence.schema}`.`{evidence.table}`"
    target_table = f"`{target.target_schema}`.`{target.table}`"
    columns, existing_columns, staged_columns = _copy_columns(
        cursor,
        evidence,
        target,
        target_exists=target_exists,
    )
    window_sql = ""
    parameters: tuple[str, ...] | None = None
    if allowed_periods:
        placeholders = ", ".join(["%s"] * len(allowed_periods))
        window_sql = f" AND existing.`{period_column}` IN ({placeholders})"
        parameters = allowed_periods
    if target_exists:
        cursor.execute(
            f"INSERT INTO {candidate} ({columns}) "
            f"SELECT {existing_columns} FROM {target_table} existing "
            "WHERE NOT EXISTS ("
            f"SELECT 1 FROM {staged} staged "
            f"WHERE staged.`{period_column}` = existing.`{period_column}`"
            f"){window_sql}",
            parameters,
        )
    staged_window_sql = ""
    if allowed_periods:
        placeholders = ", ".join(["%s"] * len(allowed_periods))
        staged_window_sql = f" WHERE staged.`{period_column}` IN ({placeholders})"
    cursor.execute(
        f"INSERT INTO {candidate} ({columns}) "
        f"SELECT {staged_columns} FROM {staged} staged{staged_window_sql}",
        parameters,
    )


def _copy_columns(
    cursor: Any,
    evidence: TableEvidence,
    target: TableTarget,
    *,
    target_exists: bool,
) -> tuple[str, str, str]:
    staged_columns = _writable_columns(cursor, evidence.schema, evidence.table)
    if target_exists:
        target_columns = _writable_columns(cursor, target.target_schema, target.table)
        if staged_columns != target_columns:
            raise ActivationError(
                "staging/target writable-column mismatch for "
                f"{evidence.table}: staging={staged_columns} target={target_columns}"
            )
    columns = ", ".join(f"`{column}`" for column in staged_columns)
    return (
        columns,
        ", ".join(f"existing.`{column}`" for column in staged_columns),
        ", ".join(f"staged.`{column}`" for column in staged_columns),
    )


def _writable_columns(
    cursor: Any,
    schema: str,
    table: str,
    *,
    required: bool = True,
) -> tuple[str, ...]:
    cursor.execute(
        "SELECT COLUMN_NAME, EXTRA FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION",
        (schema, table),
    )
    columns = tuple(
        str(column)
        for column, extra in cursor.fetchall()
        if "auto_increment" not in str(extra).casefold()
        and "generated" not in str(extra).casefold()
    )
    if not columns and required:
        raise ActivationError(f"no writable columns found for {schema}.{table}")
    return columns


def _insert_keyword_stage_candidate(
    cursor: Any,
    plan: ActivationPlan,
    evidence: TableEvidence,
    target: TableTarget,
    *,
    target_exists: bool,
) -> None:
    candidate = f"`{plan.build_schema}`.`{evidence.table}`"
    staged_table = f"`{evidence.schema}`.`{evidence.table}`"
    if not target_exists:
        cursor.execute(f"INSERT INTO {candidate} SELECT * FROM {staged_table}")
        return
    target_table = f"`{target.target_schema}`.`{target.table}`"
    columns = ", ".join(f"`{column}`" for column in _KEYWORD_STAGE_COLUMNS)
    selected = ", ".join(f"staged.`{column}`" for column in _KEYWORD_STAGE_COLUMNS)
    join = (
        f"LEFT JOIN {target_table} existing "
        "ON existing.`stage_row_sha256` = staged.`stage_row_sha256`"
    )
    cursor.execute(
        f"INSERT INTO {candidate} "
        f"SELECT * FROM {target_table} existing "
        "WHERE NOT EXISTS ("
        f"SELECT 1 FROM {staged_table} staged "
        "WHERE staged.`period_ym` = existing.`period_ym`)"
    )
    cursor.execute(
        f"INSERT INTO {candidate} (`id`, {columns}) "
        f"SELECT existing.`id`, {selected} "
        f"FROM {staged_table} staged {join} "
        "WHERE existing.`id` IS NOT NULL"
    )
    cursor.execute(
        f"INSERT INTO {candidate} ({columns}) "
        f"SELECT {selected} FROM {staged_table} staged {join} "
        "WHERE existing.`id` IS NULL"
    )


def _safe_run_id(run_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_]", "_", run_id).strip("_")
    if not safe:
        raise ActivationError("run_id must contain at least one safe character")
    return safe


def _nsa_window(epoch: str) -> tuple[str, ...]:
    match = re.fullmatch(r"(\d{4})-?Q([1-4])", epoch.strip(), flags=re.IGNORECASE)
    if match is None:
        raise ActivationError(f"invalid IQVIA NSA epoch: {epoch!r}")
    end_index = int(match.group(1)) * 4 + int(match.group(2)) - 1
    return tuple(_quarter_label(index) for index in range(end_index - 19, end_index + 1))


def _quarter_label(index: int) -> str:
    year = index // 4
    quarter = index % 4 + 1
    return f"{year}Q{quarter}"
