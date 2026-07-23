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

from pipeline.scripts.deploy.mart_load_ops import (
    PublishAction,
    publish_table_group_atomically,
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
WRITER_LOCK_NAME = "jw-market:ubist-ingest:single-writer"
GENERAL_TABLES = (
    "mart_general_brand_metric",
    "mart_general_market_metric",
)
_SCHEMA_RE = re.compile(r"^[A-Za-z0-9_]+$")
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


def acquire_writer_lock(conn: Any, *, timeout_seconds: int = 0) -> None:
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT GET_LOCK(%s, %s)", (WRITER_LOCK_NAME, timeout_seconds))
        row = cursor.fetchone()
    finally:
        cursor.close()
    value = next(iter(row.values())) if isinstance(row, dict) else row[0]
    if int(value or 0) != 1:
        raise RuntimeError(f"single-writer lock is busy: {WRITER_LOCK_NAME}")


def release_writer_lock(conn: Any) -> None:
    require_writer_lock_owner(conn)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT RELEASE_LOCK(%s)", (WRITER_LOCK_NAME,))
        cursor.fetchone()
    finally:
        cursor.close()


def require_writer_lock_owner(conn: Any) -> None:
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT IS_USED_LOCK(%s), CONNECTION_ID()",
            (WRITER_LOCK_NAME,),
        )
        row = cursor.fetchone()
    finally:
        cursor.close()
    values = list(row.values()) if isinstance(row, dict) else list(row)
    if len(values) < 2 or values[0] is None or int(values[0]) != int(values[1]):
        raise RuntimeError(f"single-writer lock ownership lost: {WRITER_LOCK_NAME}")


def build_shadow(config: MartActivation, *, ubist_dir: Path) -> None:
    previous = {key: os.environ.get(key) for key in _S4_MUTATED_ENV}
    try:
        run_s4_general(
            build_db=config.build_db,
            source_db=config.source_db,
            catalog_root=None,
            ubist_dir=ubist_dir,
            input_mode="raw",
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
) -> tuple[Any, ...]:
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
