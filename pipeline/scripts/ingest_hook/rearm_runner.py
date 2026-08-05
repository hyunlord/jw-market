"""Audited, fail-closed rearm of an intact failed publish candidate."""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from pipeline.scripts.ingest_hook import config
from pipeline.scripts.ingest_hook.ledger import STATUS_FAILED, Ledger, _utc_timestamp
from pipeline.scripts.ingest_hook.ubist_mart_activation import (
    BuildTableFingerprint,
    CorpusCandidate,
    CorpusInventory,
    _journal_child_path,
    fingerprint_build_tables,
    inventory_corpus,
    update_activation_journal,
)


class RearmRejected(RuntimeError):
    """The candidate did not satisfy the audited rearm contract."""


@dataclass(frozen=True)
class RearmResult:
    status: str
    build_run_id: str
    inventory: CorpusInventory


def _journal(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RearmRejected(f"activation journal is unreadable: {path}") from exc


def rearm(
    *, ledger: Ledger, epoch: str, category: str, manifest_sha: str,
    build_run_id: str, actor: str, expected_file_count: int,
    expected_total_bytes: int, expected_manifest_sha: str,
    read_build_fingerprints: Callable[[str], tuple[BuildTableFingerprint, ...]],
    now: datetime | None = None,
) -> RearmResult:
    identity = (epoch, category, manifest_sha)
    entry = ledger.status(*identity)
    candidate = ledger.prepared_candidate(*identity)
    if entry is None or candidate is None:
        raise RearmRejected("exact candidate identity was not found")
    if candidate.build_run_id != build_run_id or entry.run_id != build_run_id:
        raise RearmRejected("exact candidate identity does not match build run")
    if entry.status != STATUS_FAILED:
        raise RearmRejected(f"rearm requires failed ledger status, got {entry.status}")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if current > _utc_timestamp(candidate.expires_at):
        raise RearmRejected(f"candidate expired at {candidate.expires_at}")
    if not actor.strip():
        raise RearmRejected("audit actor is required")

    payload = candidate.payload
    journal_path = Path(str(payload.get("activation_journal") or ""))
    journal = _journal(journal_path)
    journal_identity = (
        str(journal.get("epoch") or ""), str(journal.get("category") or ""),
        str(journal.get("manifest_sha") or ""), str(journal.get("run_id") or ""),
    )
    if journal_identity != (*identity, build_run_id):
        raise RearmRejected("activation journal identity mismatch")
    phase = str(journal.get("phase") or "")
    if phase not in {"recovered", "rearm_started"}:
        raise RearmRejected(f"rearm requires recovered journal, got {phase!r}")
    corpus = CorpusCandidate(
        _journal_child_path(journal_path.parent, journal.get("live_root")),
        _journal_child_path(journal_path.parent, journal.get("candidate_root")),
        _journal_child_path(journal_path.parent, journal.get("backup_root")),
    )
    safe_run_id = re.sub(r"[^A-Za-z0-9_]", "_", build_run_id)
    failed_root = corpus.live_root.parent / f".{corpus.live_root.name}_failed_{safe_run_id}"
    if corpus.backup_root.exists():
        raise RearmRejected("backup corpus already exists")
    if phase == "recovered" and corpus.candidate_root.exists():
        raise RearmRejected("candidate corpus exists before audited rearm starts")
    if phase == "rearm_started" and corpus.candidate_root.exists() == failed_root.exists():
        raise RearmRejected("rearm-started corpus state is ambiguous")
    inventory_root = corpus.candidate_root if corpus.candidate_root.exists() else failed_root
    try:
        actual = inventory_corpus(inventory_root)
    except RuntimeError as exc:
        raise RearmRejected(str(exc)) from exc
    supplied = CorpusInventory(
        int(expected_file_count), int(expected_total_bytes), expected_manifest_sha,
    )
    recorded_raw = payload.get("candidate_integrity")
    if not isinstance(recorded_raw, dict):
        raise RearmRejected("candidate has no recorded corpus integrity")
    expected = CorpusInventory(
        int(recorded_raw.get("file_count", -1)),
        int(recorded_raw.get("total_bytes", -1)),
        str(recorded_raw.get("manifest_sha") or ""),
    )
    if supplied != expected:
        raise RearmRejected("operator integrity assertion does not match prepared candidate")
    if actual != expected:
        raise RearmRejected(f"corpus integrity mismatch: expected={expected} actual={actual}")
    build_db = str(journal.get("build_db") or "")
    if build_db != str(payload.get("build_db") or ""):
        raise RearmRejected("build schema identity mismatch")
    recorded_build = payload.get("build_table_integrity")
    if not isinstance(recorded_build, list):
        raise RearmRejected("candidate has no recorded build-table integrity")
    expected_build = tuple(
        (
            str(item.get("table") or ""), int(item.get("row_count", -1)),
            int(item.get("crc_sum", -1)), int(item.get("crc_xor", -1)),
        )
        for item in recorded_build
        if isinstance(item, dict)
    )
    actual_build = tuple(
        (item.table, item.row_count, item.crc_sum, item.crc_xor)
        for item in read_build_fingerprints(build_db)
    )
    if actual_build != expected_build:
        raise RearmRejected("build-table integrity mismatch")

    if phase == "recovered":
        update_activation_journal(journal_path, "rearm_started")
    if failed_root.exists():
        failed_root.rename(corpus.candidate_root)
    ledger_rearmed = False
    try:
        evidence = {
            "build_run_id": build_run_id, "file_count": actual.file_count,
            "total_bytes": actual.total_bytes, "corpus_manifest_sha": actual.manifest_sha,
        }
        if not ledger.rearm_failed_candidate(
            *identity, build_run_id=build_run_id, actor=actor, evidence=evidence
        ):
            raise RearmRejected("ledger changed before rearm could be recorded")
        ledger_rearmed = True
        update_activation_journal(journal_path, "awaiting_approval")
    except Exception:
        if ledger_rearmed:
            raise
        update_activation_journal(journal_path, "recovered")
        if corpus.candidate_root.exists() and not failed_root.exists():
            corpus.candidate_root.rename(failed_root)
        raise
    return RearmResult("awaiting_approval", build_run_id, actual)


def _read_build_fingerprints(name: str) -> tuple[BuildTableFingerprint, ...]:
    conn = config.open_mart_connection(name)
    try:
        return fingerprint_build_tables(conn, name)
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m pipeline.scripts.ingest_hook.rearm_runner")
    parser.add_argument("--epoch", required=True)
    parser.add_argument("--category", required=True)
    parser.add_argument("--manifest-sha", required=True)
    parser.add_argument("--build-run-id", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--expected-file-count", required=True, type=int)
    parser.add_argument("--expected-total-bytes", required=True, type=int)
    parser.add_argument("--expected-manifest-sha", required=True)
    args = parser.parse_args(argv)
    try:
        result = rearm(
            ledger=config.open_configured_ledger(), epoch=args.epoch, category=args.category,
            manifest_sha=args.manifest_sha, build_run_id=args.build_run_id, actor=args.actor,
            expected_file_count=args.expected_file_count,
            expected_total_bytes=args.expected_total_bytes,
            expected_manifest_sha=args.expected_manifest_sha,
            read_build_fingerprints=_read_build_fingerprints,
        )
    except RearmRejected as exc:
        print(json.dumps({"status": "rejected", "reason": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps({"status": result.status, "build_run_id": result.build_run_id}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
