"""PL-gated UBIST corpus -> isolated general mart -> atomic publish helpers."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from typing import Any, Callable

from pipeline.etl.io.catalog.paths import (
    CATALOG_ROOT_ENV,
    resolve_catalog_root,
)
from pipeline.scripts.deploy.mart_load_ops import (
    PublishAction,
    publish_table_group_atomically,
    quote_id,
    restore_table_group_atomically,
    run_s4_general,
)
from pipeline.scripts.deploy.mart_load_verify import table_exists
from pipeline.scripts.rollback.recording import (
    PromotionIdentity,
    record_mysql_component,
)


ENV_PROMOTION_APPROVED = "INGEST_MART_PROMOTION_APPROVED"
ENV_SOURCE_DB = "INGEST_MART_SOURCE_DB"
ENV_TARGET_DB = "INGEST_MART_TARGET_DB"
ENV_BUILD_PREFIX = "INGEST_MART_BUILD_PREFIX"
ENV_SHADOW_TARGET_DB = "INGEST_SHADOW_TARGET_DB"
ENV_SHADOW_BUILD_PREFIX = "INGEST_SHADOW_BUILD_PREFIX"
ENV_SHADOW_CATALOG_ROOT = "INGEST_SHADOW_CATALOG_ROOT"
ENV_CATALOG_IQVIA_NSA_DIR = "INGEST_CATALOG_IQVIA_NSA_DIR"
ENV_SHADOW_CRASH_AT = "INGEST_SHADOW_CRASH_AT"
ENV_SHADOW_FAILURE_AT = "INGEST_SHADOW_FAILURE_AT"
SHADOW_FAILURE_SIGMA_PARTS_WHOLE = "sigma_parts_whole"
SHADOW_FAILURE_POST_GATE_ROW_COUNT = "post_gate_row_count"
SHADOW_DB_PREFIX = "jw_mart_ingest_shadow_"
SHADOW_BASELINE_COPY_BATCH_SIZE = 100
WRITER_LOCK_NAME = "jw-market:ubist-ingest:single-writer"
GENERAL_TABLES = (
    "mart_general_brand_metric",
    "mart_general_market_metric",
)
_SCHEMA_RE = re.compile(r"^[A-Za-z0-9_]+$")
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_S4_MUTATED_ENV = (
    "MARIADB_DATABASE",
    "MARIADB_SOURCE_DATABASE",
    "MARIADB_USER",
    "MARIADB_PASSWORD",
    "MARIADB_HOST",
    "MARIADB_PORT",
    "HOST_PORT",
    "S4_INPUT_MODE",
    "S4_ENRICHED_DIR",
    "S4_CATALOG_DIR",
    "S4_IQVIA_NSA_DIR",
    "S4_UBIST_DIR",
)


@dataclass(frozen=True, slots=True)
class MartActivation:
    source_db: str
    target_db: str
    build_db: str


@dataclass(frozen=True, slots=True)
class CorpusCandidate:
    live_root: Path
    candidate_root: Path
    backup_root: Path


def from_env(*, run_id: str) -> MartActivation:
    if os.environ.get(ENV_PROMOTION_APPROVED, "").strip() != "1":
        raise RuntimeError(
            f"production mart activation requires {ENV_PROMOTION_APPROVED}=1 "
            "after the explicit PL gate"
        )
    source_db = os.environ.get(ENV_SOURCE_DB, "jw_mart").strip()
    target_db = os.environ.get(ENV_TARGET_DB, source_db).strip()
    prefix = os.environ.get(ENV_BUILD_PREFIX, "jw_mart_ingest").strip()
    safe_run_id = re.sub(r"[^A-Za-z0-9_]", "_", run_id)
    build_db = f"{prefix}_{safe_run_id}"
    for label, value in (
        (ENV_SOURCE_DB, source_db),
        (ENV_TARGET_DB, target_db),
        (ENV_BUILD_PREFIX, prefix),
        ("build_db", build_db),
    ):
        if not _SCHEMA_RE.fullmatch(value):
            raise RuntimeError(f"{label} is not a safe schema identifier: {value!r}")
    if build_db in {source_db, target_db, "jw_mart"}:
        raise RuntimeError("mart build schema must be isolated from source and serving schemas")
    return MartActivation(source_db, target_db, build_db)


def shadow_from_env(*, run_id: str) -> MartActivation:
    """Build an activation that cannot address the serving mart."""

    if not os.environ.get("INGEST_LOAD_SHADOW_ROOT", "").strip():
        raise RuntimeError("isolated shadow activation requires INGEST_LOAD_SHADOW_ROOT")
    source_db = os.environ.get(
        ENV_SOURCE_DB, os.environ.get("MARIADB_DATABASE", "jw_mart")
    ).strip()
    target_db = os.environ.get(ENV_SHADOW_TARGET_DB, "").strip()
    prefix = os.environ.get(ENV_SHADOW_BUILD_PREFIX, f"{SHADOW_DB_PREFIX}build").strip()
    safe_run_id = re.sub(r"[^A-Za-z0-9_]", "_", run_id)
    build_db = f"{prefix}_{safe_run_id}"
    for label, value in (
        (ENV_SOURCE_DB, source_db),
        (ENV_SHADOW_TARGET_DB, target_db),
        (ENV_SHADOW_BUILD_PREFIX, prefix),
        ("build_db", build_db),
    ):
        if not value or not _SCHEMA_RE.fullmatch(value):
            raise RuntimeError(f"isolated shadow {label} is not a safe schema identifier: {value!r}")
    if not target_db.startswith(SHADOW_DB_PREFIX) or not build_db.startswith(SHADOW_DB_PREFIX):
        raise RuntimeError(
            f"isolated shadow schemas must start with {SHADOW_DB_PREFIX!r}"
        )
    if source_db in {target_db, build_db} or target_db == build_db:
        raise RuntimeError("isolated shadow source, target, and build schemas must be distinct")
    return MartActivation(source_db, target_db, build_db)


def shadow_lock_name(target_db: str) -> str:
    if not target_db.startswith(SHADOW_DB_PREFIX):
        raise RuntimeError(f"shadow writer lock requires isolated target DB: {target_db}")
    return f"jw-market:ubist-shadow:{target_db}"


def shadow_catalog_root_from_env(shadow_root: Path) -> Path:
    value = os.environ.get(ENV_SHADOW_CATALOG_ROOT, "").strip()
    if not value:
        raise RuntimeError(f"isolated shadow build requires {ENV_SHADOW_CATALOG_ROOT}")
    root = Path(value).resolve()
    boundary = shadow_root.resolve()
    if not root.is_relative_to(boundary):
        raise RuntimeError("isolated shadow catalog must be inside the shadow root")
    return root


def production_catalog_root_from_env() -> Path:
    """Resolve the NFS production catalog; preparation validates or rebuilds it."""

    return resolve_catalog_root(_PROJECT_ROOT).resolve()


def prepare_catalog_for_mart(
    *,
    catalog_root: Path,
    ubist_dir: Path,
    source_db: str,
    conn: Any,
    run_id: str,
    output_parent: Path,
):
    """Ensure the NFS catalog matches the current MI Master before S4 starts."""

    from pipeline.etl.io.iqvia_loader import DEFAULT_NSA_PARQUET_DIR
    from pipeline.etl.lib.storage import get_mi_master_path
    from pipeline.scripts.ingest_hook.catalog_refresh import ensure_nfs_catalog

    iqvia_nsa_dir = Path(
        os.environ.get(ENV_CATALOG_IQVIA_NSA_DIR, str(DEFAULT_NSA_PARQUET_DIR))
    )
    return ensure_nfs_catalog(
        catalog_root=catalog_root,
        mi_master=get_mi_master_path(),
        ubist_dir=ubist_dir,
        iqvia_nsa_dir=iqvia_nsa_dir,
        target_db=source_db,
        conn=conn,
        run_id=run_id,
        output_parent=output_parent,
    )


def ensure_shadow_target_baseline(conn: Any, config: MartActivation) -> None:
    """Atomically seed the isolated publish baseline from the serving source."""

    if (
        not config.target_db.startswith(SHADOW_DB_PREFIX)
        or not config.build_db.startswith(SHADOW_DB_PREFIX)
        or config.target_db in {config.source_db, "jw_mart"}
    ):
        raise RuntimeError(
            f"isolated shadow baseline refused target DB: {config.target_db}"
        )
    cursor = conn.cursor()
    scratch_tables: list[str] = []
    try:
        cursor.execute(f"CREATE DATABASE IF NOT EXISTS {quote_id(config.target_db)}")
        existing = tuple(
            table_exists(conn, config.target_db, table) for table in GENERAL_TABLES
        )
        if all(existing):
            return
        if any(existing):
            raise RuntimeError(
                f"shadow baseline is partially initialized: {config.target_db}"
            )
        moves: list[str] = []
        for table in GENERAL_TABLES:
            if not table_exists(conn, config.source_db, table):
                raise RuntimeError(
                    f"shadow baseline source is missing: {config.source_db}.{table}"
                )
            scratch = f"{table}__shadow_seed"
            if table_exists(conn, config.target_db, scratch):
                raise RuntimeError(
                    f"shadow baseline scratch table already exists: "
                    f"{config.target_db}.{scratch}"
                )
            scratch_tables.append(scratch)
            cursor.execute(
                f"CREATE TABLE {quote_id(config.target_db)}.{quote_id(scratch)} LIKE "
                f"{quote_id(config.source_db)}.{quote_id(table)}"
            )
            cursor.execute(
                f"SELECT COUNT(*), COALESCE(MAX(`id`), 0) FROM "
                f"{quote_id(config.source_db)}.{quote_id(table)}"
            )
            source_row = cursor.fetchone()
            source_values = (
                list(source_row.values())
                if isinstance(source_row, dict)
                else list(source_row)
            )
            source_rows, source_max_id = map(int, source_values[:2])
            last_id = 0
            while last_id < source_max_id:
                inserted = cursor.execute(
                    f"INSERT INTO {quote_id(config.target_db)}.{quote_id(scratch)} SELECT * FROM "
                    f"{quote_id(config.source_db)}.{quote_id(table)} WHERE `id` > %s "
                    f"AND `id` <= %s ORDER BY `id` LIMIT "
                    f"{SHADOW_BASELINE_COPY_BATCH_SIZE}",
                    (last_id, source_max_id),
                )
                if int(inserted or 0) <= 0:
                    raise RuntimeError(
                        f"shadow baseline copy made no progress for {table} after id {last_id}"
                    )
                conn.commit()
                cursor.execute(
                    f"SELECT COALESCE(MAX(`id`), 0) FROM "
                    f"{quote_id(config.target_db)}.{quote_id(scratch)}"
                )
                max_row = cursor.fetchone()
                new_last_id = int(
                    next(iter(max_row.values()))
                    if isinstance(max_row, dict)
                    else max_row[0]
                )
                if new_last_id <= last_id:
                    raise RuntimeError(
                        f"shadow baseline copy did not advance for {table}: "
                        f"{new_last_id} <= {last_id}"
                    )
                last_id = new_last_id
            cursor.execute(
                f"SELECT COUNT(*) FROM {quote_id(config.target_db)}.{quote_id(scratch)}"
            )
            copied_row = cursor.fetchone()
            copied_rows = next(iter(copied_row.values())) if isinstance(copied_row, dict) else copied_row[0]
            if int(copied_rows) != int(source_rows):
                raise RuntimeError(
                    f"shadow baseline copy mismatch for {table}: "
                    f"{copied_rows} != {source_rows}"
                )
            moves.append(
                f"{quote_id(config.target_db)}.{quote_id(scratch)} TO "
                f"{quote_id(config.target_db)}.{quote_id(table)}"
            )
        cursor.execute("RENAME TABLE " + ", ".join(moves))
        conn.commit()
    except Exception:
        rollback = getattr(conn, "rollback", None)
        if callable(rollback):
            rollback()
        for scratch in scratch_tables:
            if table_exists(conn, config.target_db, scratch):
                cursor.execute(
                    f"DROP TABLE {quote_id(config.target_db)}.{quote_id(scratch)}"
                )
        raise
    finally:
        cursor.close()


def acquire_writer_lock(
    conn: Any, *, timeout_seconds: int = 0, lock_name: str = WRITER_LOCK_NAME
) -> None:
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT GET_LOCK(%s, %s)", (lock_name, timeout_seconds))
        row = cursor.fetchone()
    finally:
        cursor.close()
    value = next(iter(row.values())) if isinstance(row, dict) else row[0]
    if int(value or 0) != 1:
        raise RuntimeError(f"single-writer lock is busy: {lock_name}")


def release_writer_lock(conn: Any, *, lock_name: str = WRITER_LOCK_NAME) -> None:
    require_writer_lock_owner(conn, lock_name=lock_name)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT RELEASE_LOCK(%s)", (lock_name,))
        cursor.fetchone()
    finally:
        cursor.close()


def require_writer_lock_owner(conn: Any, *, lock_name: str = WRITER_LOCK_NAME) -> None:
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT IS_USED_LOCK(%s), CONNECTION_ID()",
            (lock_name,),
        )
        row = cursor.fetchone()
    finally:
        cursor.close()
    values = list(row.values()) if isinstance(row, dict) else list(row)
    if len(values) < 2 or values[0] is None or int(values[0]) != int(values[1]):
        raise RuntimeError(f"single-writer lock ownership lost: {lock_name}")


def maybe_inject_shadow_crash(point: str) -> None:
    """Raise only at an explicitly configured shadow-only recovery boundary."""

    configured = os.environ.get(ENV_SHADOW_CRASH_AT, "").strip()
    if not configured:
        return
    if not os.environ.get("INGEST_LOAD_SHADOW_ROOT", "").strip():
        raise RuntimeError(f"{ENV_SHADOW_CRASH_AT} is valid in shadow mode only")
    if configured == point:
        raise RuntimeError(f"deterministic shadow crash injected at {point}")


def _shadow_failure_at() -> str:
    point = os.environ.get(ENV_SHADOW_FAILURE_AT, "").strip()
    if not point:
        return ""
    if not os.environ.get("INGEST_LOAD_SHADOW_ROOT", "").strip():
        raise RuntimeError(f"{ENV_SHADOW_FAILURE_AT} is valid in shadow mode only")
    allowed = {
        SHADOW_FAILURE_SIGMA_PARTS_WHOLE,
        SHADOW_FAILURE_POST_GATE_ROW_COUNT,
    }
    if point not in allowed:
        raise RuntimeError(f"unsupported {ENV_SHADOW_FAILURE_AT} value: {point}")
    return point


def shadow_post_gate_actual_rows(actual_rows: int) -> int:
    """Return a deterministic row-count mismatch for an isolated gate exercise."""

    if _shadow_failure_at() == SHADOW_FAILURE_POST_GATE_ROW_COUNT:
        return actual_rows + 1
    return actual_rows


def maybe_inject_shadow_sigma_mismatch(
    conn: Any, *, source: str, periods: tuple[str, ...]
) -> dict[str, str] | None:
    """Corrupt one disposable build-row so the real Sigma gate must reject it."""

    if _shadow_failure_at() != SHADOW_FAILURE_SIGMA_PARTS_WHOLE:
        return None
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT DATABASE()")
        database_row = cursor.fetchone()
        database = (
            next(iter(database_row.values()))
            if isinstance(database_row, dict)
            else database_row[0]
        )
        if not str(database).startswith(SHADOW_DB_PREFIX):
            raise RuntimeError(
                f"shadow Sigma injection refused non-isolated database: {database}"
            )
        cursor.execute(
            "SELECT brand_key, atc4_code, metric_history "
            "FROM mart_general_brand_metric "
            "WHERE source=%s AND measure='sales' ORDER BY atc4_code, brand_key",
            (source,),
        )
        while row := cursor.fetchone():
            if isinstance(row, dict):
                brand_key = str(row["brand_key"])
                atc4_code = str(row["atc4_code"])
                raw_history = row["metric_history"]
            else:
                brand_key, atc4_code, raw_history = str(row[0]), str(row[1]), row[2]
            try:
                history = json.loads(raw_history) if raw_history else {}
            except (TypeError, ValueError):
                continue
            if not isinstance(history, dict):
                continue
            for period in periods:
                entry = history.get(period)
                raw_value = entry.get("raw_value") if isinstance(entry, dict) else None
                if not isinstance(raw_value, (int, float)):
                    continue
                entry["raw_value"] = (
                    float(raw_value)
                    + max(abs(float(raw_value)), 1.0) * 1000
                    + 1e12
                )
                cursor.execute(
                    "UPDATE mart_general_brand_metric SET metric_history=%s "
                    "WHERE source=%s AND measure='sales' AND atc4_code=%s AND brand_key=%s",
                    (
                        json.dumps(history, ensure_ascii=False, separators=(",", ":")),
                        source,
                        atc4_code,
                        brand_key,
                    ),
                )
                conn.commit()
                return {
                    "atc4_code": atc4_code,
                    "brand_key": brand_key,
                    "period": period,
                }
        raise RuntimeError(
            f"shadow Sigma injection found no numeric brand cell for periods={periods}"
        )
    finally:
        cursor.close()


def build_shadow(
    config: MartActivation, *, catalog_root: Path | None, ubist_dir: Path
) -> None:
    previous = {key: os.environ.get(key) for key in _S4_MUTATED_ENV}
    try:
        run_s4_general(
            build_db=config.build_db,
            source_db=config.source_db,
            catalog_root=catalog_root,
            ubist_dir=ubist_dir,
            input_mode="raw",
            sources=("ubist",),
        )
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def prepare_candidate_corpus(live_root: Path, *, run_id: str) -> CorpusCandidate:
    """Clone the current corpus so loaders never mutate the serving root in place."""

    safe_run_id = re.sub(r"[^A-Za-z0-9_]", "_", run_id)
    candidate = live_root.parent / f".{live_root.name}_candidate_{safe_run_id}"
    backup = live_root.parent / f".{live_root.name}_backup_{safe_run_id}"
    if not live_root.is_dir():
        raise RuntimeError(f"live UBIST corpus is missing: {live_root}")
    if candidate.exists() or backup.exists():
        raise RuntimeError(f"corpus scratch path already exists for run_id={run_id}")
    shutil.copytree(live_root, candidate)
    return CorpusCandidate(live_root, candidate, backup)


def ensure_shadow_corpus(shadow_live_root: Path, *, seed_root: Path) -> None:
    """Atomically seed an absent isolated corpus from the read-only production corpus."""

    shadow = shadow_live_root.resolve()
    seed = seed_root.resolve()
    if shadow == seed or shadow in seed.parents or seed in shadow.parents:
        raise RuntimeError("shadow corpus must be physically separate from its seed corpus")
    if shadow.exists():
        corpus_manifest_sha(shadow)
        return
    if not seed.is_dir():
        raise RuntimeError(f"shadow seed corpus is missing: {seed}")
    shadow.parent.mkdir(parents=True, exist_ok=True)
    temp = shadow.parent / f".{shadow.name}_seed_tmp"
    if temp.exists():
        raise RuntimeError(f"shadow seed scratch path already exists: {temp}")
    shutil.copytree(seed, temp)
    corpus_manifest_sha(temp)
    temp.rename(shadow)


def corpus_manifest_sha(root: Path) -> str:
    manifest = root / "_manifest.json"
    if not manifest.is_file():
        raise RuntimeError(f"UBIST corpus manifest is missing: {manifest}")
    return hashlib.sha256(manifest.read_bytes()).hexdigest()


def require_corpus_manifest(root: Path, expected_sha: str) -> None:
    actual = corpus_manifest_sha(root)
    if actual != expected_sha:
        raise RuntimeError(
            f"UBIST corpus changed while candidate was built: {actual} != {expected_sha}"
        )


def promote_candidate_corpus(corpus: CorpusCandidate) -> None:
    corpus.live_root.rename(corpus.backup_root)
    try:
        corpus.candidate_root.rename(corpus.live_root)
    except Exception:
        corpus.backup_root.rename(corpus.live_root)
        raise


def rollback_candidate_corpus(corpus: CorpusCandidate) -> None:
    failed = _failed_corpus_path(corpus)
    if not corpus.backup_root.exists():
        if corpus.live_root.exists() and failed.exists():
            return
        if corpus.candidate_root.exists():
            shutil.rmtree(corpus.candidate_root)
        return
    if corpus.live_root.exists():
        if failed.exists():
            raise RuntimeError(f"corpus rollback scratch path already exists: {failed}")
        corpus.live_root.rename(failed)
    corpus.backup_root.rename(corpus.live_root)


def activation_journal_path(corpus: CorpusCandidate, *, run_id: str) -> Path:
    safe_run_id = re.sub(r"[^A-Za-z0-9_]", "_", run_id)
    return corpus.live_root.parent / f".ubist_activation_{safe_run_id}.json"


def write_activation_journal(
    corpus: CorpusCandidate,
    config: MartActivation,
    *,
    run_id: str,
    phase: str,
    identity: tuple[str, str, str],
) -> Path:
    epoch, category, manifest_sha = identity
    path = activation_journal_path(corpus, run_id=run_id)
    payload = {
        "version": 2,
        "run_id": run_id,
        "phase": phase,
        "epoch": epoch,
        "category": category,
        "manifest_sha": manifest_sha,
        "source_db": config.source_db,
        "target_db": config.target_db,
        "build_db": config.build_db,
        "live_root": str(corpus.live_root),
        "candidate_root": str(corpus.candidate_root),
        "backup_root": str(corpus.backup_root),
        "tables": list(GENERAL_TABLES),
    }
    _atomic_write_json(path, payload)
    return path


def update_activation_journal(path: Path, phase: str) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["phase"] = phase
    _atomic_write_json(path, payload)


def recover_incomplete_activations(
    conn: Any,
    *,
    output_root: Path,
    ledger_status: Callable[[str, str, str], str | None] | None = None,
    required_target_prefix: str | None = None,
) -> tuple[Path, ...]:
    """Restore interrupted corpus/table promotions; caller must refresh old caches."""

    recovered: list[Path] = []
    for path in sorted(output_root.glob(".ubist_activation_*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        phase = str(payload.get("phase") or "")
        if phase in {"complete", "recovered"}:
            continue
        run_id = str(payload.get("run_id") or "")
        target_db = str(payload.get("target_db") or "")
        tables = tuple(str(value) for value in payload.get("tables") or ())
        if not run_id or not _SCHEMA_RE.fullmatch(target_db) or tables != GENERAL_TABLES:
            raise RuntimeError(f"invalid activation recovery journal: {path}")
        if required_target_prefix and not target_db.startswith(required_target_prefix):
            raise RuntimeError(
                f"activation recovery target escapes required prefix "
                f"{required_target_prefix!r}: {target_db}"
            )
        identity = (
            str(payload.get("epoch") or ""),
            str(payload.get("category") or ""),
            str(payload.get("manifest_sha") or ""),
        )
        if ledger_status is not None and all(identity):
            status = ledger_status(*identity)
            if status == "complete":
                if phase not in {"refresh_succeeded", "ledger_complete", "signal_complete"}:
                    raise RuntimeError(
                        f"ledger is complete before activation reached refresh success: {path}"
                    )
                update_activation_journal(path, "complete")
                continue
        corpus = CorpusCandidate(
            _journal_child_path(output_root, payload.get("live_root")),
            _journal_child_path(output_root, payload.get("candidate_root")),
            _journal_child_path(output_root, payload.get("backup_root")),
        )
        actions = tuple(
            PublishAction(
                table,
                "atomic_group_rename",
                table,
                f"{table}__old_{run_id}",
                0,
            )
            for table in tables
        )
        recovery_run_id = f"recovery_{run_id}"
        backup_exists = tuple(
            table_exists(conn, target_db, str(action.backup_table)) for action in actions
        )
        if any(backup_exists) and not all(backup_exists):
            raise RuntimeError(f"ambiguous partial mart backup state for journal: {path}")
        failed_exists = tuple(
            table_exists(conn, target_db, f"{action.table}__failed_{recovery_run_id}")
            for action in actions
        )
        serving_exists = tuple(
            table_exists(conn, target_db, action.table) for action in actions
        )
        if all(backup_exists):
            update_activation_journal(path, "recovery_mart_started")
            restore_table_group_atomically(
                conn,
                target_db=target_db,
                actions=actions,
                run_id=recovery_run_id,
            )
            update_activation_journal(path, "recovery_mart_complete")
            phase = "recovery_mart_complete"
        elif phase == "recovery_mart_started":
            if not all(failed_exists) or not all(serving_exists):
                raise RuntimeError(f"mart recovery state is not resumable: {path}")
            update_activation_journal(path, "recovery_mart_complete")
            phase = "recovery_mart_complete"
        elif phase in {
            "mart_promoted",
            "refresh_started",
            "refresh_succeeded",
            "ledger_complete",
            "signal_complete",
        }:
            raise RuntimeError(f"mart promotion journal has no restorable backups: {path}")
        if corpus.backup_root.exists():
            update_activation_journal(path, "recovery_corpus_started")
            rollback_candidate_corpus(corpus)
        elif phase == "recovery_corpus_started":
            if not corpus.live_root.exists() or not _failed_corpus_path(corpus).exists():
                raise RuntimeError(f"corpus recovery state is not resumable: {path}")
        elif phase in {
            "corpus_promoted",
            "mart_promoted",
            "refresh_started",
            "refresh_succeeded",
            "ledger_complete",
            "signal_complete",
            "recovery_mart_complete",
        }:
            raise RuntimeError(f"corpus promotion journal has no restorable backup: {path}")
        update_activation_journal(path, "rollback_needs_refresh")
        recovered.append(path)
    return tuple(recovered)


def complete_recovery(paths: tuple[Path, ...]) -> None:
    for path in paths:
        update_activation_journal(path, "recovered")


def _journal_child_path(root: Path, value: object) -> Path:
    path = Path(str(value)).resolve()
    resolved_root = root.resolve()
    if path.parent != resolved_root:
        raise RuntimeError(f"activation journal path escapes output root: {path}")
    return path


def _failed_corpus_path(corpus: CorpusCandidate) -> Path:
    suffix = corpus.backup_root.name.rsplit("_", 1)[-1]
    return corpus.live_root.parent / f".{corpus.live_root.name}_failed_{suffix}"


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    with temp.open("wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    temp.replace(path)
    directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def require_completed_post_gate(conn: Any, *, ingest_run_id: str) -> None:
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT status, reason FROM ingest_stage_event "
            "WHERE run_id=%s AND stage='post_gate' ORDER BY id DESC LIMIT 1",
            (ingest_run_id,),
        )
        row = cursor.fetchone()
    finally:
        cursor.close()
    if row is None:
        raise RuntimeError(f"promotion blocked: post_gate absent for ingest_run_id={ingest_run_id}")
    status = str(row.get("status") if isinstance(row, dict) else row[0])
    reason = row.get("reason") if isinstance(row, dict) else row[1]
    if status != "complete":
        raise RuntimeError(
            f"promotion blocked: ingest_run_id={ingest_run_id} post_gate={status} reason={reason}"
        )


def publish_shadow(
    conn: Any,
    config: MartActivation,
    *,
    run_id: str,
    epoch: str,
    ingest_run_id: str,
    require_ledger_gate: bool = True,
) -> tuple[Any, ...]:
    if require_ledger_gate:
        require_completed_post_gate(conn, ingest_run_id=ingest_run_id)
    actions = publish_table_group_atomically(
        conn,
        build_db=config.build_db,
        target_db=config.target_db,
        run_id=run_id,
        tables=GENERAL_TABLES,
    )
    try:
        record_mysql_component(
            conn,
            identity=PromotionIdentity(
                promotion_run_id=run_id,
                epoch=epoch,
                ingest_run_id=ingest_run_id,
                serving_db=config.target_db,
                generation_db=config.build_db,
            ),
            component="general",
            table_pairs=tuple(
                (action.table, action.backup_table)
                for action in actions
                if action.backup_table is not None
            ),
        )
    except Exception:
        restore_table_group_atomically(
            conn,
            target_db=config.target_db,
            actions=actions,
            run_id=run_id,
        )
        raise
    return actions


def validate_shadow_publish(conn: Any, config: MartActivation) -> dict[str, int]:
    """Read back the isolated publish without invoking serving cache builders."""

    if not config.target_db.startswith(SHADOW_DB_PREFIX):
        raise RuntimeError(f"shadow refresh refused non-isolated target DB: {config.target_db}")
    counts: dict[str, int] = {}
    cursor = conn.cursor()
    try:
        for table in GENERAL_TABLES:
            if not table_exists(conn, config.target_db, table):
                raise RuntimeError(f"shadow refresh target is missing: {config.target_db}.{table}")
            cursor.execute(f"SELECT COUNT(*) FROM `{config.target_db}`.`{table}`")
            row = cursor.fetchone()
            value = next(iter(row.values())) if isinstance(row, dict) else row[0]
            counts[table] = int(value)
    finally:
        cursor.close()
    if any(value < 1 for value in counts.values()):
        raise RuntimeError(f"shadow refresh found empty general mart tables: {counts}")
    return counts
