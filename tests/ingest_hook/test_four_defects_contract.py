from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ingest_fixtures import FakeTransport
from pipeline.scripts.ingest_hook import job_runner
from pipeline.scripts.ingest_hook.app import IngestService, create_app
from pipeline.scripts.ingest_hook.source_inventory import (
    FileObservation,
    SourceSetEvidence,
)


def _record_required_stages(
    ledger,
    identity: tuple[str, str, str],
    *,
    run_id: str,
    missing: str | None = None,
) -> None:
    required = job_runner._SOURCE_STAGE_CONTRACTS[identity[1]]
    for seq, stage in enumerate(required, start=1):
        if stage == missing:
            continue
        ledger.record_stage(
            *identity,
            run_id=run_id,
            seq=seq,
            stage=stage,
            status="complete",
            reason="test evidence",
            started_at="2026-08-12T00:00:00+00:00",
            finished_at="2026-08-12T00:00:01+00:00",
            duration_ms=1,
        )


def test_required_stage_gate_rejects_false_complete(sqlite_ledger) -> None:
    identity = ("2026-Q1", "iqvia_nsa", "a" * 64)
    sqlite_ledger.receive(*identity, manifest_path="/input/manifest.json")
    sqlite_ledger.mark_running(*identity, job_name="nsa", run_id="run-1")
    _record_required_stages(
        sqlite_ledger,
        identity,
        run_id="run-1",
        missing="dashboard",
    )

    with pytest.raises(
        job_runner.RequiredStageContractError,
        match="missing_required_stages=dashboard",
    ):
        job_runner._mark_complete_after_required_stages(
            ledger=sqlite_ledger,
            identity=identity,
            run_ids=("run-1",),
            row_counts={"iqvia_nsa_quarterly_raw": 1},
        )

    assert sqlite_ledger.status(*identity).status == "running"


def test_required_stage_gate_marks_complete_only_after_all_stages(sqlite_ledger) -> None:
    identity = ("2026-Q1", "iqvia_nsa", "b" * 64)
    sqlite_ledger.receive(*identity, manifest_path="/input/manifest.json")
    sqlite_ledger.mark_running(*identity, job_name="nsa", run_id="run-1")
    _record_required_stages(sqlite_ledger, identity, run_id="run-1")

    job_runner._mark_complete_after_required_stages(
        ledger=sqlite_ledger,
        identity=identity,
        run_ids=("run-1",),
        row_counts={"iqvia_nsa_quarterly_raw": 1},
    )

    assert sqlite_ledger.status(*identity).status == "complete"


def test_production_run_does_not_bypass_required_stage_gate() -> None:
    source = inspect.getsource(job_runner.run)

    assert source.count("ledger.mark_complete(*identity") == 1
    assert "if mode == \"production\":\n                _mark_complete_after_required_stages(" in source


def test_complete_terminal_validates_running_before_queue_promotion(
    sqlite_ledger,
) -> None:
    identity = ("2026-Q1", "iqvia_nsa", "c" * 64)
    sqlite_ledger.receive(*identity, manifest_path="/input/manifest.json")
    sqlite_ledger.mark_running(*identity, job_name="nsa", run_id="run-1")
    transport = FakeTransport()
    client = TestClient(create_app(IngestService(sqlite_ledger, None, transport=transport)))
    payload = {
        "schema_version": "1",
        "event_id": "7d77770d-7a77-5777-8777-777777777777",
        "run_id": "run-1",
        "event": "complete",
        "mode": "production",
        "source": "iqvia_nsa",
        "epoch": identity[0],
        "period": identity[0],
        "target_schema": "jw_mart",
        "published_at": "2026-08-12 00:00:00",
        "occurred_at": "2026-08-12T00:00:00+00:00",
        "manifest_sha": identity[2],
        "rows_before": 1,
        "rows_after": 1,
        "rows_loaded": 0,
        "started_at": "2026-08-12T00:00:00+00:00",
        "finished_at": "2026-08-12T00:00:01+00:00",
        "failure_reason": None,
        "log_ref": "/ingest/status",
    }

    response = client.post("/ingest/terminal", json=payload)

    assert response.status_code == 200
    assert response.json()["terminal_status"] == "running"
    assert response.json()["promoted_job_name"] is None
    assert sqlite_ledger.status(*identity).status == "running"


def test_publish_source_set_is_remeasured_with_load_hash_definition(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "NSA.xlsx"
    source.write_bytes(b"source bytes")
    expected = job_runner._sha256_file(source)
    load_evidence = SourceSetEvidence(
        sha256="0" * 64,
        relative_paths=(source.name,),
        rows=12,
        periods=("2026-Q1",),
    )
    policy = type("Policy", (), {"root": tmp_path})()
    monkeypatch.setattr(job_runner, "load_scan_policy", lambda *_args, **_kwargs: policy)

    measured = job_runner._measure_publish_source_set("iqvia_nsa", load_evidence)

    expected_evidence = job_runner.source_set_evidence(
        (
            FileObservation(
                source.name,
                expected,
                source.stat().st_size,
                "classified",
                category="iqvia_nsa",
                rows=12,
                periods=("2026-Q1",),
            ),
        )
    )
    assert measured == expected_evidence
