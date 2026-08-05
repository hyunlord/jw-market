"""Audited, fail-closed rearm of an intact failed publish candidate."""
from __future__ import annotations

import argparse
import hashlib
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


def _validate_legacy_corpus_manifest(
    root: Path, *, epoch: str, category: str, build_run_id: str,
) -> dict[str, object]:
    """Cross-check a pre-integrity candidate against its internal build records."""
    try:
        import pyarrow.parquet as pq

        manifest_path = root / "_manifest.json"
        report_path = root / "post_gate_report.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (ImportError, OSError, ValueError) as exc:
        raise RearmRejected("legacy corpus manifest is unreadable") from exc
    if (
        report.get("status") != "pass"
        or str(report.get("epoch") or "") != epoch
        or str(report.get("category") or "") != category
        or str(report.get("run_id") or "") != build_run_id
    ):
        raise RearmRejected("legacy post-gate report identity or status mismatch")
    partitions = manifest.get("partitions")
    if not isinstance(partitions, list) or not partitions:
        raise RearmRejected("legacy corpus manifest has no partitions")
    expected: dict[str, int] = {}
    for item in partitions:
        if not isinstance(item, dict):
            raise RearmRejected("legacy corpus manifest contains an invalid partition")
        relative = str(item.get("path") or "")
        path = (root / relative).resolve()
        try:
            path.relative_to(root.resolve())
            rows = int(item["row_count"])
        except (KeyError, TypeError, ValueError):
            raise RearmRejected("legacy corpus manifest contains an invalid row count")
        if not relative or path.suffix != ".parquet" or relative in expected or rows < 0:
            raise RearmRejected("legacy corpus manifest contains an invalid partition path")
        expected[relative] = rows
    actual_paths = {
        item.relative_to(root).as_posix()
        for item in root.rglob("*.parquet")
        if item.is_file()
    }
    if actual_paths != set(expected):
        raise RearmRejected("legacy corpus parquet paths do not match its manifest")
    total_rows = 0
    for relative, expected_rows in expected.items():
        actual_rows = int(pq.ParquetFile(root / relative).metadata.num_rows)
        if actual_rows != expected_rows:
            raise RearmRejected(
                f"legacy corpus row count mismatch for {relative}: "
                f"expected={expected_rows} actual={actual_rows}"
            )
        total_rows += actual_rows
    return {
        "manifest_file_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "partition_count": len(expected),
        "parquet_row_count": total_rows,
        "post_gate_status": "pass",
    }


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
    allow_legacy_integrity_reconstruction: bool = False,
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
    legacy_evidence: dict[str, object] | None = None
    if isinstance(recorded_raw, dict):
        expected = CorpusInventory(
            int(recorded_raw.get("file_count", -1)),
            int(recorded_raw.get("total_bytes", -1)),
            str(recorded_raw.get("manifest_sha") or ""),
        )
        if supplied != expected:
            raise RearmRejected("operator integrity assertion does not match prepared candidate")
        if actual != expected:
            raise RearmRejected(f"corpus integrity mismatch: expected={expected} actual={actual}")
    else:
        if not allow_legacy_integrity_reconstruction:
            raise RearmRejected("legacy integrity reconstruction requires explicit opt-in")
        if supplied != actual:
            raise RearmRejected("operator integrity assertion does not match legacy corpus")
        legacy_evidence = _validate_legacy_corpus_manifest(
            inventory_root, epoch=epoch, category=category, build_run_id=build_run_id,
        )
    build_db = str(journal.get("build_db") or "")
    if build_db != str(payload.get("build_db") or ""):
        raise RearmRejected("build schema identity mismatch")
    actual_build_items = read_build_fingerprints(build_db)
    actual_build = tuple(
        (item.table, item.row_count, item.crc_sum, item.crc_xor)
        for item in actual_build_items
    )
    recorded_build = payload.get("build_table_integrity")
    if isinstance(recorded_build, list):
        expected_build = tuple(
            (
                str(item.get("table") or ""), int(item.get("row_count", -1)),
                int(item.get("crc_sum", -1)), int(item.get("crc_xor", -1)),
            )
            for item in recorded_build
            if isinstance(item, dict)
        )
        if actual_build != expected_build:
            raise RearmRejected("build-table integrity mismatch")
    elif legacy_evidence is None:
        raise RearmRejected("candidate has no recorded build-table integrity")

    if phase == "recovered":
        update_activation_journal(journal_path, "rearm_started")
    if failed_root.exists():
        failed_root.rename(corpus.candidate_root)
    ledger_rearmed = False
    try:
        evidence = {
            "build_run_id": build_run_id, "file_count": actual.file_count,
            "total_bytes": actual.total_bytes, "corpus_manifest_sha": actual.manifest_sha,
            "integrity_origin": (
                "legacy_manifest_reconstruction" if legacy_evidence is not None
                else "prepared_candidate"
            ),
        }
        integrity_updates: dict[str, object] = {}
        if legacy_evidence is not None:
            evidence["legacy_manifest"] = legacy_evidence
            integrity_updates = {
                "candidate_integrity": {
                    "file_count": actual.file_count,
                    "total_bytes": actual.total_bytes,
                    "manifest_sha": actual.manifest_sha,
                },
                "build_table_integrity": [
                    {
                        "table": item.table, "row_count": item.row_count,
                        "crc_sum": item.crc_sum, "crc_xor": item.crc_xor,
                    }
                    for item in actual_build_items
                ],
            }
        if not ledger.rearm_failed_candidate(
            *identity, build_run_id=build_run_id, actor=actor, evidence=evidence,
            integrity_updates=integrity_updates,
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
    parser.add_argument("--allow-legacy-integrity-reconstruction", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = rearm(
            ledger=config.open_configured_ledger(), epoch=args.epoch, category=args.category,
            manifest_sha=args.manifest_sha, build_run_id=args.build_run_id, actor=args.actor,
            expected_file_count=args.expected_file_count,
            expected_total_bytes=args.expected_total_bytes,
            expected_manifest_sha=args.expected_manifest_sha,
            read_build_fingerprints=_read_build_fingerprints,
            allow_legacy_integrity_reconstruction=args.allow_legacy_integrity_reconstruction,
        )
    except RearmRejected as exc:
        print(json.dumps({"status": "rejected", "reason": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps({"status": result.status, "build_run_id": result.build_run_id}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
