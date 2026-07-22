from __future__ import annotations

import json

import pytest

from pipeline.scripts.ingest_hook import job_runner
from pipeline.scripts.ingest_hook.completion_signal import CompletionSignal, publish
from pipeline.scripts.ingest_hook.completion_signal import PublishResult
from pipeline.scripts.ingest_hook.contract import load_manifest
from ingest_fixtures import write_submission


class Response:
    def __init__(self, status: int):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


def _signal(rows: int = 7) -> CompletionSignal:
    return CompletionSignal(
        event="complete",
        mode="staging",
        category="iqvia_csd_keyword",
        epoch="2026-03",
        manifest_sha="a" * 64,
        rows_before=10,
        rows_after=17,
        rows_loaded=rows,
        period_from="2026-01",
        period_to="2026-03",
        started_at="2026-07-22T00:00:00Z",
        finished_at="2026-07-22T00:01:00Z",
        failure_reason=None,
        log_ref="/ingest/status?x=1",
    )


def test_v5_complete_payload_has_frozen_identity_and_counts():
    payload = _signal().as_dict()
    assert payload["idempotency_key"] == ["2026-03", "iqvia_csd_keyword", "a" * 64]
    assert payload["rows_after"] - payload["rows_before"] == payload["rows_loaded"]


def test_v9_non_2xx_retries_with_exponential_backoff():
    calls = []
    sleeps = []

    def opener(request, timeout):
        calls.append(json.loads(request.data))
        return Response(503 if len(calls) < 4 else 204)

    result = publish(_signal(), endpoint="https://receiver.invalid/events", attempts=4, opener=opener, sleeper=sleeps.append)
    assert result.status == "published"
    assert result.attempts == 4
    assert sleeps == [1.0, 2.0, 4.0]
    assert all(payload["rows_loaded"] == 7 for payload in calls)


def test_v10_final_webhook_failure_is_reported_not_raised():
    def opener(_request, _timeout):
        raise OSError("receiver down")

    result = publish(_signal(), endpoint="https://receiver.invalid/events", attempts=3, opener=opener, sleeper=lambda _: None)
    assert result.status == "failed"
    assert result.attempts == 3
    assert "receiver down" in (result.reason or "")


def test_empty_endpoint_is_explicitly_disabled():
    result = publish(_signal(), endpoint="", attempts=3)
    assert result.status == "disabled"
    assert result.attempts == 0


def test_v5_success_signal_is_recorded_and_exposed(sqlite_ledger, bucket, tmp_path):
    manifest_path = write_submission(bucket)
    rc = job_runner.run(
        manifest_path, input_root=bucket, ledger=sqlite_ledger,
        rehearsal_root=tmp_path / "staging",
    )
    assert rc == 0
    manifest = load_manifest(manifest_path)
    events = sqlite_ledger.signal_events(manifest.epoch, manifest.category, manifest.manifest_sha)
    assert len(events) == 1
    assert events[0].event == "complete"
    assert events[0].mode == "staging"
    assert events[0].rows_loaded == 6
    assert events[0].delivery_status == "disabled"


def test_v10_delivery_failure_does_not_break_success(monkeypatch, sqlite_ledger, bucket, tmp_path):
    monkeypatch.setattr(
        "pipeline.scripts.ingest_hook.completion_signal.publish",
        lambda *_args, **_kwargs: PublishResult("failed", 4, "receiver down"),
    )
    manifest_path = write_submission(bucket)
    rc = job_runner.run(
        manifest_path, input_root=bucket, ledger=sqlite_ledger,
        rehearsal_root=tmp_path / "staging",
    )
    manifest = load_manifest(manifest_path)
    assert rc == 0
    assert sqlite_ledger.status(manifest.epoch, manifest.category, manifest.manifest_sha).status == "complete"
    signal = sqlite_ledger.signal_events(manifest.epoch, manifest.category, manifest.manifest_sha)[0]
    assert signal.delivery_status == "failed"
    assert signal.reason == "receiver down"


def test_v7_unexpected_load_failure_emits_failed_signal(
    monkeypatch, sqlite_ledger, bucket, tmp_path
):
    manifest_path = write_submission(bucket)
    monkeypatch.setattr(
        "pipeline.scripts.ingest_hook.job_runner._rehearsal_load",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("broken parser")),
    )

    rc = job_runner.run(
        manifest_path,
        input_root=bucket,
        ledger=sqlite_ledger,
        rehearsal_root=tmp_path / "staging",
    )

    manifest = load_manifest(manifest_path)
    assert rc == 1
    assert sqlite_ledger.status(*(
        manifest.epoch, manifest.category, manifest.manifest_sha
    )).status == "failed"
    signal = sqlite_ledger.signal_events(
        manifest.epoch, manifest.category, manifest.manifest_sha
    )[0]
    assert signal.event == "failed"
    assert "broken parser" in (signal.payload["failure_reason"] or "")


def test_v11_same_identity_rejects_rows_loaded_drift(sqlite_ledger):
    signal = _signal()
    payload = signal.as_dict()
    identity = (signal.epoch, signal.category, signal.manifest_sha)
    sqlite_ledger.record_signal(
        *identity, run_id="r1", event="complete", mode="staging", rows_loaded=7,
        delivery_status="failed", attempts=4, reason="down", payload=payload,
    )
    with pytest.raises(ValueError, match="count drift"):
        sqlite_ledger.record_signal(
            *identity, run_id="r2", event="complete", mode="staging", rows_loaded=8,
            delivery_status="published", attempts=1, reason=None, payload=payload,
        )


def test_v11_same_identity_rejects_rows_loaded_drift_across_event_types(sqlite_ledger):
    signal = _signal()
    identity = (signal.epoch, signal.category, signal.manifest_sha)
    sqlite_ledger.record_signal(
        *identity, run_id="r1", event="failed", mode="staging", rows_loaded=7,
        delivery_status="failed", attempts=4, reason="down", payload=signal.as_dict(),
    )
    with pytest.raises(ValueError, match="count drift"):
        sqlite_ledger.record_signal(
            *identity, run_id="r2", event="complete", mode="staging", rows_loaded=0,
            delivery_status="published", attempts=1, reason=None, payload=signal.as_dict(),
        )


def test_retry_reuses_first_signal_counts_across_failed_then_complete(monkeypatch, sqlite_ledger):
    signal = _signal()
    identity = (signal.epoch, signal.category, signal.manifest_sha)
    sqlite_ledger.record_signal(
        *identity, run_id="r1", event="failed", mode="staging", rows_loaded=7,
        delivery_status="failed", attempts=4, reason="down", payload=signal.as_dict(),
    )
    published = []
    monkeypatch.setattr(
        "pipeline.scripts.ingest_hook.completion_signal.publish",
        lambda value, **_kwargs: published.append(value.as_dict()) or PublishResult("published", 1, None),
    )

    job_runner._emit_completion_signal(
        ledger=sqlite_ledger,
        tracker=type("Tracker", (), {"complete": lambda *_args, **_kwargs: None})(),
        identity=identity,
        run_id="r2",
        event="complete",
        mode="staging",
        rows_before=7,
        rows_after=7,
        rows_loaded=0,
        periods={"2026-03"},
        started_at="2026-07-22T00:00:00Z",
        failure_reason=None,
    )

    assert published[0]["rows_before"] == 10
    assert published[0]["rows_after"] == 17
    assert published[0]["rows_loaded"] == 7


def test_replace_loader_counts_are_emitted_without_delta_recalculation(
    monkeypatch, sqlite_ledger
):
    identity = ("2026-03", "iqvia_csd_keyword", "b" * 64)
    published = []
    monkeypatch.setattr(
        "pipeline.scripts.ingest_hook.completion_signal.publish",
        lambda value, **_kwargs: published.append(value.as_dict())
        or PublishResult("published", 1, None),
    )

    job_runner._emit_completion_signal(
        ledger=sqlite_ledger,
        tracker=type("Tracker", (), {"complete": lambda *_args, **_kwargs: None})(),
        identity=identity,
        run_id="replace-1",
        event="complete",
        mode="staging",
        rows_before=20,
        rows_after=7,
        rows_loaded=7,
        periods={"2026-03"},
        started_at="2026-07-22T00:00:00Z",
        failure_reason=None,
    )

    assert published[0]["rows_before"] == 20
    assert published[0]["rows_after"] == 7
    assert published[0]["rows_loaded"] == 7
