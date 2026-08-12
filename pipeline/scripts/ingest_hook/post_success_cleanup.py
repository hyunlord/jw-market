"""Post-success retention cleanup for ingest-created mart generations.

The cleanup path is deliberately fail-closed: it records the exact drop plan
before mutating anything and only accepts exact schema/table names from the
promotion ledger.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Literal, Protocol

from pipeline.scripts.deploy.mart_load_verify import quote_id
from pipeline.scripts.rollback.ledger import PromotionLedger


IdentifierKind = Literal["schema", "table"]

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]+$")
_ROLLBACK_TABLE_RE = re.compile(r"^.+__(?:old|rollback|failed)_[A-Za-z0-9_]+$")
_DEFAULT_EVIDENCE_DIR = Path("/tmp/ingest_post_success_cleanup")
_GIB = 1024**3


class CleanupSafetyError(RuntimeError):
    """Cleanup refused to run because a safety gate failed."""


class CleanupExecutor(Protocol):
    def drop_schema(self, schema: str) -> None: ...

    def drop_tables(self, schema: str, tables: tuple[str, ...]) -> None: ...

    def sleep(self, seconds: float) -> None: ...


@dataclass(frozen=True, slots=True)
class CleanupTarget:
    kind: IdentifierKind
    schema: str
    table: str | None
    estimated_bytes: int

    @property
    def name(self) -> str:
        return self.schema if self.kind == "schema" else str(self.table)

    def validate(self, *, serving_db: str) -> None:
        _require_identifier(self.schema, label="schema")
        if self.kind == "schema":
            if self.table is not None:
                raise CleanupSafetyError("schema cleanup target must not include table")
            if self.schema == serving_db:
                raise CleanupSafetyError("refusing to drop serving schema")
            return
        if self.table is None:
            raise CleanupSafetyError("table cleanup target requires table name")
        _require_identifier(self.table, label="table")
        if self.schema != serving_db:
            raise CleanupSafetyError("rollback table cleanup must stay inside serving schema")
        if _ROLLBACK_TABLE_RE.fullmatch(self.table) is None:
            raise CleanupSafetyError("table cleanup target lacks strict rollback suffix")


@dataclass(frozen=True, slots=True)
class CleanupPlan:
    source: str
    run_id: str
    serving_db: str
    retained_generations: tuple[str, ...]
    targets: tuple[CleanupTarget, ...]
    total_estimated_bytes: int


@dataclass(frozen=True, slots=True)
class CleanupResult:
    dry_run: bool
    plan_path: Path
    plan_sha256: str
    dropped: tuple[CleanupTarget, ...]


class MySQLCleanupExecutor:
    def __init__(self, connection) -> None:
        self._connection = connection

    def drop_schema(self, schema: str) -> None:
        with self._connection.cursor() as cursor:
            cursor.execute(f"DROP DATABASE {quote_id(schema)}")
        self._connection.commit()

    def drop_tables(self, schema: str, tables: tuple[str, ...]) -> None:
        if not tables:
            return
        rendered = ", ".join(f"{quote_id(schema)}.{quote_id(table)}" for table in tables)
        with self._connection.cursor() as cursor:
            cursor.execute(f"DROP TABLE {rendered}")
        self._connection.commit()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def run_post_success_cleanup(
    connection,
    *,
    serving_db: str,
    source: str,
    run_id: str,
) -> CleanupResult:
    """Build and execute retention cleanup after a source publish fully succeeds."""
    keep_rollback_generations = _env_int("INGEST_CLEANUP_KEEP_ROLLBACK_GENERATIONS", 1)
    max_drop_bytes = _env_gib("INGEST_CLEANUP_MAX_DROP_GIB", 40)
    toi_interval_seconds = _env_float("INGEST_CLEANUP_TOI_INTERVAL_SECONDS", 30.0)
    dry_run = _env_bool("INGEST_CLEANUP_DRY_RUN", False)
    evidence_dir = Path(os.environ.get("INGEST_CLEANUP_EVIDENCE_DIR", str(_DEFAULT_EVIDENCE_DIR)))
    disk_path = Path(os.environ.get("INGEST_CLEANUP_DISK_PATH", "/"))

    if _mysql_table_exists(connection, serving_db, "promotion_generation"):
        ledger = PromotionLedger(connection, dialect="mysql", schema_db=serving_db)
        plan = build_retention_cleanup_plan(
            ledger,
            serving_db=serving_db,
            source=source,
            run_id=run_id,
            keep_verified_rollback_generations=keep_rollback_generations,
            size_lookup=_mysql_size_lookup(connection),
        )
    else:
        plan = CleanupPlan(
            source=source,
            run_id=run_id,
            serving_db=serving_db,
            retained_generations=(serving_db,),
            targets=(),
            total_estimated_bytes=0,
        )
    return execute_cleanup_plan(
        plan,
        executor=MySQLCleanupExecutor(connection),
        evidence_dir=evidence_dir,
        dry_run=dry_run,
        max_drop_bytes=max_drop_bytes,
        disk_usage_pct=lambda: _filesystem_usage_pct(disk_path),
        toi_interval_seconds=toi_interval_seconds,
    )


def build_retention_cleanup_plan(
    ledger: PromotionLedger,
    *,
    serving_db: str,
    source: str,
    run_id: str,
    keep_verified_rollback_generations: int,
    size_lookup: Callable[[CleanupTarget], int],
) -> CleanupPlan:
    if keep_verified_rollback_generations < 1:
        raise CleanupSafetyError("retention must keep at least one verified rollback generation")
    generations = ledger.generations()
    current_generation_dbs = tuple(
        row.generation_db
        for row in generations
        if row.promotion_run_id == run_id and row.generation_db
    )
    ordered_generation_dbs = tuple(
        dict.fromkeys((*current_generation_dbs, *(row.generation_db for row in generations if row.generation_db)))
    )
    retained_generation_count = keep_verified_rollback_generations + 1
    retained_generations = tuple(
        dict.fromkeys((*ordered_generation_dbs[:retained_generation_count], serving_db))
    )
    retained_set = set(retained_generations)
    targets: list[CleanupTarget] = []
    for generation in generations:
        if generation.generation_db in retained_set:
            continue
        schema_target = CleanupTarget("schema", generation.generation_db, None, 0)
        schema_target.validate(serving_db=serving_db)
        schema_target = CleanupTarget(
            schema_target.kind,
            schema_target.schema,
            schema_target.table,
            size_lookup(schema_target),
        )
        targets.append(schema_target)
        for backups in ledger.components(generation.promotion_run_id).values():
            for backup in backups:
                table_target = CleanupTarget(
                    "table",
                    serving_db,
                    backup.backup_table,
                    0,
                )
                table_target.validate(serving_db=serving_db)
                targets.append(
                    CleanupTarget(
                        table_target.kind,
                        table_target.schema,
                        table_target.table,
                        size_lookup(table_target),
                    )
                )
    unique_targets = _deduplicate_targets(tuple(targets))
    return CleanupPlan(
        source=source,
        run_id=run_id,
        serving_db=serving_db,
        retained_generations=retained_generations,
        targets=unique_targets,
        total_estimated_bytes=sum(target.estimated_bytes for target in unique_targets),
    )


def execute_cleanup_plan(
    plan: CleanupPlan,
    *,
    executor: CleanupExecutor,
    evidence_dir: Path,
    dry_run: bool,
    max_drop_bytes: int,
    disk_usage_pct: Callable[[], int],
    toi_interval_seconds: float = 30.0,
) -> CleanupResult:
    _require_disk("preflight", disk_usage_pct(), limit=72)
    for target in plan.targets:
        target.validate(serving_db=plan.serving_db)
    if plan.total_estimated_bytes > max_drop_bytes:
        raise CleanupSafetyError(
            f"estimated cleanup bytes {plan.total_estimated_bytes} exceeds cleanup cap {max_drop_bytes}"
        )
    plan_path, plan_sha = _persist_plan(plan, evidence_dir)
    if dry_run:
        return CleanupResult(True, plan_path, plan_sha, ())
    dropped: list[CleanupTarget] = []
    for target in plan.targets:
        _require_disk("runtime", disk_usage_pct(), limit=80)
        if target.kind == "schema":
            executor.drop_schema(target.schema)
        else:
            executor.drop_tables(target.schema, (str(target.table),))
        dropped.append(target)
        executor.sleep(toi_interval_seconds)
    _require_disk("runtime", disk_usage_pct(), limit=80)
    return CleanupResult(False, plan_path, plan_sha, tuple(dropped))


def _persist_plan(plan: CleanupPlan, evidence_dir: Path) -> tuple[Path, str]:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    path = evidence_dir / f"post_success_cleanup_{plan.source}_{plan.run_id}.jsonl"
    payloads = []
    for target in plan.targets:
        row = {
            "source": plan.source,
            "run_id": plan.run_id,
            "serving_db": plan.serving_db,
            "retained_generations": plan.retained_generations,
            "target": asdict(target),
        }
        payloads.append(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    path.write_text("\n".join(payloads) + ("\n" if payloads else ""), encoding="utf-8")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    sidecar = path.with_suffix(path.suffix + ".sha256")
    sidecar.write_text(f"{digest}  {path.name}\n", encoding="utf-8")
    reread = hashlib.sha256(path.read_bytes()).hexdigest()
    if reread != digest:
        raise CleanupSafetyError("cleanup plan evidence reread hash mismatch")
    return path, digest


def _deduplicate_targets(targets: tuple[CleanupTarget, ...]) -> tuple[CleanupTarget, ...]:
    by_key: dict[tuple[str, str, str | None], CleanupTarget] = {}
    for target in targets:
        key = (target.kind, target.schema, target.table)
        previous = by_key.get(key)
        if previous is not None and previous.estimated_bytes != target.estimated_bytes:
            raise CleanupSafetyError(f"conflicting cleanup size estimate for {key}")
        by_key[key] = target
    return tuple(by_key[key] for key in sorted(by_key))


def _mysql_size_lookup(connection) -> Callable[[CleanupTarget], int]:
    def lookup(target: CleanupTarget) -> int:
        with connection.cursor() as cursor:
            if target.kind == "schema":
                cursor.execute(
                    "SELECT COALESCE(SUM(data_length + index_length), 0) AS bytes "
                    "FROM information_schema.TABLES WHERE table_schema=%s",
                    (target.schema,),
                )
            else:
                cursor.execute(
                    "SELECT COALESCE(SUM(data_length + index_length), 0) AS bytes "
                    "FROM information_schema.TABLES WHERE table_schema=%s AND table_name=%s",
                    (target.schema, target.table),
                )
            row = cursor.fetchone()
        if row is None:
            return 0
        raw = row.get("bytes") if isinstance(row, dict) else row[0]
        return int(raw or 0)

    return lookup


def _mysql_table_exists(connection, schema: str, table: str) -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM information_schema.TABLES "
            "WHERE table_schema=%s AND table_name=%s LIMIT 1",
            (schema, table),
        )
        return cursor.fetchone() is not None


def _require_identifier(value: str, *, label: str) -> None:
    if _IDENTIFIER_RE.fullmatch(value) is None:
        raise CleanupSafetyError(f"unsafe {label} identifier: {value!r}")


def _require_disk(phase: str, usage_pct: int, *, limit: int) -> None:
    if usage_pct > limit:
        raise CleanupSafetyError(f"{phase} disk usage {usage_pct}% exceeds {limit}%")


def _filesystem_usage_pct(path: Path) -> int:
    usage = shutil.disk_usage(path)
    return round(usage.used * 100 / usage.total)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return default if raw is None or raw.strip() == "" else int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return default if raw is None or raw.strip() == "" else float(raw)


def _env_gib(name: str, default_gib: int) -> int:
    return _env_int(name, default_gib) * _GIB
