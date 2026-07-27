"""Ledger rejection is terminal, non-retryable, and distinct from failures."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pipeline.scripts.ingest_hook import sweep as sweep_module
from pipeline.scripts.ingest_hook.app import IngestService, create_app
from pipeline.scripts.ingest_hook.contract import Manifest, ManifestFile
from pipeline.scripts.ingest_hook.ledger import (
    STATUS_FAILED,
    STATUS_REJECTED,
    UnknownLedgerStatusError,
    is_retryable_status,
    is_terminal_status,
)

IDENTITY = ("2026-07", "ubist", "a" * 64)


def _manifest() -> Manifest:
    return Manifest(
        contract_version="v2",
        epoch=IDENTITY[0],
        category=IDENTITY[1],
        complete=True,
        files=(ManifestFile(path="demo.xlsx", sha256="b" * 64),),
        manifest_path="/input/manifest.json",
        manifest_sha=IDENTITY[2],
    )


def _one_manifest(monkeypatch: pytest.MonkeyPatch, manifest: Manifest) -> None:
    monkeypatch.setattr(
        sweep_module,
        "_iter_manifests",
        lambda _root, _s3: iter(
            [(Path(manifest.manifest_path), lambda: manifest)]
        ),
    )


def _seed_running(sqlite_ledger) -> None:
    sqlite_ledger.receive(*IDENTITY, manifest_path="/input/manifest.json")
    sqlite_ledger.mark_running(*IDENTITY, job_name="job-a", run_id="run-a")


def test_rejected_is_terminal_and_not_retryable(sqlite_ledger) -> None:
    _seed_running(sqlite_ledger)
    sqlite_ledger.mark_rejected(*IDENTITY, reason="CorrectionRejected: changed bytes")

    entry = sqlite_ledger.status(*IDENTITY)
    retry = sqlite_ledger.receive(*IDENTITY, manifest_path="/input/retry.json")

    assert entry is not None
    assert entry.status == STATUS_REJECTED
    assert is_terminal_status(entry.status)
    assert not is_retryable_status(entry.status)
    assert retry.action == "noop"
    assert retry.status == STATUS_REJECTED
    assert sqlite_ledger.status(*IDENTITY).status == STATUS_REJECTED
    print(
        json.dumps(
            {
                "case": "rejected_retry_blocked",
                "status": entry.status,
                "terminal": True,
                "retryable": False,
                "receive_action": retry.action,
            }
        )
    )


def test_loader_failure_remains_failed_and_retryable(sqlite_ledger) -> None:
    _seed_running(sqlite_ledger)
    sqlite_ledger.mark_failed(*IDENTITY, reason="InjectedLoaderError: boom")

    entry = sqlite_ledger.status(*IDENTITY)

    assert entry is not None
    assert entry.status == STATUS_FAILED
    assert entry.status != STATUS_REJECTED
    assert is_terminal_status(entry.status)
    assert is_retryable_status(entry.status)
    print(
        json.dumps(
            {
                "case": "loader_failure",
                "status": entry.status,
                "reason": entry.reason,
            }
        )
    )


def test_sweep_does_not_recover_rejected_identity(
    sqlite_ledger,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _manifest()
    _seed_running(sqlite_ledger)
    sqlite_ledger.mark_rejected(*IDENTITY, reason="CorrectionRejected: changed bytes")
    _one_manifest(monkeypatch, manifest)
    promotions: list[str] = []
    monkeypatch.setattr(
        sweep_module.IngestService,
        "promote",
        lambda _service, category: promotions.append(category),
    )

    result = sweep_module.sweep(sqlite_ledger, tmp_path)

    assert result["found"] == 0
    assert result["kicked"] == 0
    assert result["failed"] == 0
    assert promotions == []
    assert sqlite_ledger.status(*IDENTITY).status == STATUS_REJECTED
    print(json.dumps({"case": "rejected_sweep", **result}, default=str))


def test_unknown_status_fails_closed(sqlite_ledger) -> None:
    sqlite_ledger.receive(*IDENTITY, manifest_path="/input/manifest.json")
    sqlite_ledger._conn.execute(  # test-only corruption injection
        "UPDATE ingest_ledger SET status=? WHERE epoch=? AND category=? AND manifest_sha=?",
        ("future_status", *IDENTITY),
    )
    sqlite_ledger._conn.commit()

    with pytest.raises(UnknownLedgerStatusError, match="future_status"):
        sqlite_ledger.status(*IDENTITY)
    print(json.dumps({"case": "unknown_status", "verdict": "fail_closed"}))


def test_rejection_does_not_modify_existing_rows(sqlite_ledger) -> None:
    preserved = ("2026-06", "iqvia_nsa", "c" * 64)
    sqlite_ledger.receive(*preserved, manifest_path="/input/preserved.json")
    sqlite_ledger.mark_complete(*preserved, row_counts={"preserved.xlsx": 7})
    before = sqlite_ledger.status(*preserved)

    _seed_running(sqlite_ledger)
    sqlite_ledger.mark_rejected(*IDENTITY, reason="CorrectionRejected: changed bytes")

    after = sqlite_ledger.status(*preserved)
    assert after == before
    print(
        json.dumps(
            {
                "case": "existing_rows_untouched",
                "before": before.__dict__,
                "after": after.__dict__,
                "existing_count_before": 1,
                "existing_count_after": 1,
                "exact_match": True,
            },
            default=str,
        )
    )


def test_status_api_exposes_rejected_status_and_reason(
    sqlite_ledger,
    tmp_path: Path,
) -> None:
    reason = "CorrectionRejected: changed bytes"
    _seed_running(sqlite_ledger)
    sqlite_ledger.mark_rejected(*IDENTITY, reason=reason)
    client = TestClient(create_app(IngestService(sqlite_ledger, tmp_path)))

    response = client.get(
        "/ingest/status",
        params={
            "epoch": IDENTITY[0],
            "category": IDENTITY[1],
            "manifest_sha": IDENTITY[2],
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == STATUS_REJECTED
    assert response.json()["reason"] == reason
