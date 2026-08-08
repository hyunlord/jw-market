from __future__ import annotations

import json
from dataclasses import replace

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
        source="iqvia_csd_keyword",
        epoch="2026-03",
        manifest_sha="a" * 64,
        run_id="run-1",
        target_schema="jw_brand_activity_stage",
        published_at="2026-07-22 00:01:00",
        occurred_at="2026-07-22 00:01:00",
        rows_before=10,
        rows_after=17,
        rows_loaded=rows,
        period_from="2026-01",
        period_to="2026-03",
        started_at="2026-07-22T00:00:00Z",
        finished_at="2026-07-22T00:01:00Z",
        failure_reason=None,
        log_ref="/ingest/status?x=1",
        affected_scope={"dimension": "atc4", "count": 1, "values": ["M1A1"]},
    )


def test_v5_complete_payload_has_frozen_identity_and_counts():
    payload = _signal().as_dict()
    assert payload["schema_version"] == "1"
    assert payload["source"] == "iqvia_csd_keyword"
    assert "category" not in payload
    assert "source_family" not in payload
    assert payload["period"] == "2026-03"
    assert payload["period_range"] == {"from": "2026-01", "to": "2026-03"}
    assert payload["event_id"]
    assert payload["affected_scope"] == {
        "dimension": "atc4",
        "count": 1,
        "values": ["M1A1"],
    }
    assert payload["rows_after"] - payload["rows_before"] == payload["rows_loaded"]


@pytest.mark.parametrize(
    "source",
    ["ubist", "iqvia_nsa", "iqvia_csd_channel", "iqvia_csd_keyword"],
)
def test_v1_source_preserves_internal_category_key_without_mapping(source):
    payload = replace(_signal(), source=source).as_dict()

    assert payload["source"] == source


def test_v1_rejects_unknown_source_without_silently_mapping_it():
    with pytest.raises(ValueError, match="unsupported completion source"):
        replace(_signal(), source="CSD")


def test_v1_rejects_prepared_outbound_event():
    with pytest.raises(ValueError, match="unsupported completion event"):
        replace(_signal(), event="prepared")


def test_v1_failed_allows_null_published_at():
    payload = replace(_signal(), event="failed", published_at=None).as_dict()

    assert payload["published_at"] is None


def test_v1_event_id_is_stable_for_same_event_and_differs_for_another_run():
    first = _signal().as_dict()["event_id"]
    repeated = _signal().as_dict()["event_id"]
    another_run = replace(_signal(), run_id="run-2").as_dict()["event_id"]

    assert first == repeated
    assert first != another_run


def test_v9_non_2xx_retries_with_exponential_backoff():
    calls = []
    sleeps = []

    def opener(request, data=None, timeout=None):
        assert data is None
        assert timeout == 15
        calls.append(json.loads(request.data))
        return Response(503 if len(calls) < 4 else 204)

    result = publish(_signal(), endpoint="https://receiver.invalid/events", attempts=4, opener=opener, sleeper=sleeps.append)
    assert result.status == "published"
    assert result.attempts == 4
    assert sleeps == [1.0, 2.0, 4.0]
    assert all(payload["rows_loaded"] == 7 for payload in calls)
    assert len({payload["event_id"] for payload in calls}) == 1


def test_v10_final_webhook_failure_is_reported_not_raised():
    def opener(_request, data=None, timeout=None):
        assert data is None
        assert timeout == 15
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
    stage = sqlite_ledger.stage_events(manifest.epoch, manifest.category, manifest.manifest_sha)[-1]
    assert stage.stage == "signal"
    assert stage.status == "failed"


def test_signal_delivery_is_recorded_pending_before_send_then_final(
    monkeypatch, sqlite_ledger
):
    identity = ("2026-03", "iqvia_csd_keyword", "b" * 64)
    writes: list[str] = []
    original = sqlite_ledger.record_signal

    def record(*args, **kwargs):
        writes.append(kwargs["delivery_status"])
        return original(*args, **kwargs)

    monkeypatch.setattr(sqlite_ledger, "record_signal", record)
    monkeypatch.setattr(
        "pipeline.scripts.ingest_hook.completion_signal.publish",
        lambda *_args, **_kwargs: PublishResult("failed", 3, "receiver down"),
    )
    tracker = type(
        "Tracker",
        (),
        {
            "complete": lambda *_args, **_kwargs: None,
            "skip": lambda *_args, **_kwargs: None,
            "record_failure": lambda *_args, **_kwargs: None,
        },
    )()

    job_runner._emit_completion_signal(
        ledger=sqlite_ledger,
        tracker=tracker,
        identity=identity,
        run_id="run-1",
        event="failed",
        mode="staging",
        rows_before=20,
        rows_after=7,
        rows_loaded=7,
        periods={"2026-03"},
        started_at="2026-07-22T00:00:00Z",
        failure_reason="broken",
    )

    assert writes == ["pending", "failed"]


def test_disabled_delivery_records_signal_stage_complete(
    monkeypatch, sqlite_ledger
):
    identity = ("2026-03", "iqvia_csd_keyword", "c" * 64)
    completed: list[tuple[str, str | None]] = []
    failed: list[tuple[str, str]] = []

    monkeypatch.setattr(
        "pipeline.scripts.ingest_hook.completion_signal.publish",
        lambda *_args, **_kwargs: PublishResult(
            "disabled", 0, "completion endpoint is not configured"
        ),
    )
    tracker = type(
        "Tracker",
        (),
        {
            "complete": lambda _self, stage, reason=None: completed.append(
                (stage, reason)
            ),
            "record_failure": lambda _self, stage, reason: failed.append(
                (stage, reason)
            ),
        },
    )()

    job_runner._emit_completion_signal(
        ledger=sqlite_ledger,
        tracker=tracker,
        identity=identity,
        run_id="run-disabled",
        event="complete",
        mode="production",
        rows_before=10,
        rows_after=12,
        rows_loaded=2,
        periods={"2026-03"},
        started_at="2026-08-08T00:00:00Z",
        failure_reason=None,
    )

    assert failed == []
    assert len(completed) == 1
    assert completed[0][0] == "signal"
    assert "delivery=disabled" in (completed[0][1] or "")


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
    with pytest.raises(ValueError, match="event count drift"):
        sqlite_ledger.record_signal(
            *identity, run_id="r2", event="complete", mode="staging", rows_loaded=8,
            delivery_status="published", attempts=1, reason=None, payload=payload,
        )


def test_v11_retry_allows_count_change_between_failed_and_prepared_events(sqlite_ledger):
    signal = _signal()
    identity = (signal.epoch, signal.category, signal.manifest_sha)
    sqlite_ledger.record_signal(
        *identity, run_id="r1", event="failed", mode="staging", rows_loaded=0,
        delivery_status="failed", attempts=4, reason="down", payload=signal.as_dict(),
    )
    sqlite_ledger.record_signal(
        *identity, run_id="r2", event="prepared", mode="staging", rows_loaded=2174,
        delivery_status="suppressed", attempts=0, reason=None,
        payload={"event": "prepared", "outbound": False},
    )

    assert [event.rows_loaded for event in sqlite_ledger.signal_events(*identity)] == [0, 2174]


def test_retry_does_not_reuse_failed_signal_counts_for_complete(monkeypatch, sqlite_ledger):
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

    assert published[0]["rows_before"] == 7
    assert published[0]["rows_after"] == 7
    assert published[0]["rows_loaded"] == 0


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
