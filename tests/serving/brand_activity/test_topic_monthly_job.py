from __future__ import annotations

import json
from datetime import datetime, timezone
from types import TracebackType

import pytest

from pipeline.scripts.deploy.brand_activity_307 import topic_monthly_job


def test_preflight_fails_safe_when_seed_is_missing() -> None:
    """Given no successful fingerprint seed, When preflight runs, Then execution is blocked."""
    current = topic_monthly_job.StageFingerprint(row_count=29346, stage_hash_fingerprint="fp-current")

    decision = topic_monthly_job.decide_preflight(current, None)

    assert decision.action is topic_monthly_job.PreflightAction.FAIL
    assert "seed missing" in decision.message


def test_preflight_noops_when_input_is_unchanged() -> None:
    """Given matching current and stored fingerprints, When preflight runs, Then no GenOS call starts."""
    current = topic_monthly_job.StageFingerprint(row_count=29346, stage_hash_fingerprint="fp-current")
    stored = topic_monthly_job.StoredFingerprint(run_id="brand_activity_replay_20260702_160109", input_fingerprint="fp-current")

    decision = topic_monthly_job.decide_preflight(current, stored)

    assert decision.action is topic_monthly_job.PreflightAction.NOOP
    assert "input unchanged" in decision.message


def test_preflight_starts_when_input_changed() -> None:
    """Given a new stage fingerprint, When preflight runs, Then the scheduler may start one run."""
    current = topic_monthly_job.StageFingerprint(row_count=30000, stage_hash_fingerprint="fp-next")
    stored = topic_monthly_job.StoredFingerprint(run_id="brand_activity_replay_20260702_160109", input_fingerprint="fp-current")

    decision = topic_monthly_job.decide_preflight(current, stored)

    assert decision.action is topic_monthly_job.PreflightAction.START
    assert decision.current.stage_hash_fingerprint == "fp-next"


def test_main_records_normal_noop_without_starting_topic_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unchanged producer input leaves a durable B outcome and still exits zero."""
    observations: list[tuple[str, str]] = []
    current = topic_monthly_job.StageFingerprint(
        row_count=29346,
        stage_hash_fingerprint="fp-current",
    )
    stored = topic_monthly_job.StoredFingerprint(
        run_id="previous-run",
        input_fingerprint="fp-current",
    )
    monkeypatch.setattr(topic_monthly_job, "_config_from_env", topic_monthly_job.JobConfig)
    monkeypatch.setattr(topic_monthly_job, "connect_mariadb", lambda: _Connection())
    monkeypatch.setattr(topic_monthly_job, "fetch_stage_fingerprint", lambda *_args, **_kwargs: current)
    monkeypatch.setattr(topic_monthly_job, "fetch_last_stored_fingerprint", lambda *_args, **_kwargs: stored)
    monkeypatch.setattr(
        topic_monthly_job,
        "_record_producer_observation",
        lambda *, status, fingerprint, reason: observations.append((status, reason)),
    )
    monkeypatch.setattr(
        topic_monthly_job,
        "run_topic_job",
        lambda *_args, **_kwargs: pytest.fail("no-op must not start a topic run"),
    )

    assert topic_monthly_job.main() == 0
    assert observations == [
        (topic_monthly_job.PRODUCER_NOOP, "input_unchanged"),
    ]


def test_main_records_failure_before_returning_existing_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing seed leaves a durable C outcome without changing the producer exit."""
    observations: list[tuple[str, str]] = []
    current = topic_monthly_job.StageFingerprint(
        row_count=29346,
        stage_hash_fingerprint="fp-current",
    )
    monkeypatch.setattr(topic_monthly_job, "_config_from_env", topic_monthly_job.JobConfig)
    monkeypatch.setattr(topic_monthly_job, "connect_mariadb", lambda: _Connection())
    monkeypatch.setattr(topic_monthly_job, "fetch_stage_fingerprint", lambda *_args, **_kwargs: current)
    monkeypatch.setattr(topic_monthly_job, "fetch_last_stored_fingerprint", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        topic_monthly_job,
        "_record_producer_observation",
        lambda *, status, fingerprint, reason: observations.append((status, reason)),
    )

    assert topic_monthly_job.main() == 1
    assert observations == [
        (topic_monthly_job.PRODUCER_FAILED, "fingerprint_seed_missing"),
    ]


def test_main_records_started_then_complete_without_changing_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful producer run keeps exit zero and publishes its lifecycle markers."""
    observations: list[tuple[str, str]] = []
    current = topic_monthly_job.StageFingerprint(
        row_count=30000,
        stage_hash_fingerprint="fp-next",
    )
    stored = topic_monthly_job.StoredFingerprint(
        run_id="previous-run",
        input_fingerprint="fp-current",
    )
    result = topic_monthly_job.RunResult(
        run_id="new-run",
        status="done",
        executed_call_count=3,
        artifact_sha256="artifact",
        db_save_summary={"stored_run_rows": 1, "stored_topic_rows": 2},
        raw_payload={},
    )
    monkeypatch.setattr(topic_monthly_job, "_config_from_env", topic_monthly_job.JobConfig)
    monkeypatch.setattr(topic_monthly_job, "connect_mariadb", lambda: _Connection())
    monkeypatch.setattr(topic_monthly_job, "fetch_stage_fingerprint", lambda *_args, **_kwargs: current)
    monkeypatch.setattr(topic_monthly_job, "fetch_last_stored_fingerprint", lambda *_args, **_kwargs: stored)
    monkeypatch.setattr(topic_monthly_job, "run_topic_job", lambda *_args, **_kwargs: result)
    monkeypatch.setattr(
        topic_monthly_job,
        "_record_producer_observation",
        lambda *, status, fingerprint, reason: observations.append((status, reason)),
    )

    assert topic_monthly_job.main() == 0
    assert observations == [
        (topic_monthly_job.PRODUCER_STARTED, "topic_run_started"),
        (topic_monthly_job.PRODUCER_COMPLETE, "topic_run_complete"),
    ]


def test_main_records_interrupted_run_before_reraising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A producer exception leaves C evidence while preserving the existing failure."""
    observations: list[tuple[str, str]] = []
    current = topic_monthly_job.StageFingerprint(
        row_count=30000,
        stage_hash_fingerprint="fp-next",
    )
    stored = topic_monthly_job.StoredFingerprint(
        run_id="previous-run",
        input_fingerprint="fp-current",
    )
    monkeypatch.setattr(topic_monthly_job, "_config_from_env", topic_monthly_job.JobConfig)
    monkeypatch.setattr(topic_monthly_job, "connect_mariadb", lambda: _Connection())
    monkeypatch.setattr(topic_monthly_job, "fetch_stage_fingerprint", lambda *_args, **_kwargs: current)
    monkeypatch.setattr(topic_monthly_job, "fetch_last_stored_fingerprint", lambda *_args, **_kwargs: stored)
    monkeypatch.setattr(
        topic_monthly_job,
        "run_topic_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(topic_monthly_job.SchedulerError("injected")),
    )
    monkeypatch.setattr(
        topic_monthly_job,
        "_record_producer_observation",
        lambda *, status, fingerprint, reason: observations.append((status, reason)),
    )

    with pytest.raises(topic_monthly_job.SchedulerError, match="injected"):
        topic_monthly_job.main()
    assert observations == [
        (topic_monthly_job.PRODUCER_STARTED, "topic_run_started"),
        (topic_monthly_job.PRODUCER_FAILED, "topic_run_exception"),
    ]


def test_monthly_observation_key_is_stable_within_utc_month() -> None:
    observed_at = datetime(2026, 8, 4, 10, 0, tzinfo=timezone.utc)

    assert topic_monthly_job._observation_run_id(observed_at) == (
        "monthly-axis-observation:2026-08"
    )


def test_observation_row_cannot_match_pending_assignment_states(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = _Cursor()
    monkeypatch.setattr(topic_monthly_job, "connect_mariadb", lambda: _Connection(cursor))
    monkeypatch.setattr(topic_monthly_job, "_config_from_env", topic_monthly_job.JobConfig)
    monkeypatch.setattr(
        topic_monthly_job,
        "_observation_run_id",
        lambda _observed_at=None: "monthly-axis-observation:2026-08",
    )

    topic_monthly_job._record_producer_observation(
        status=topic_monthly_job.PRODUCER_NOOP,
        fingerprint="input-fingerprint",
        reason="input_unchanged",
    )

    assert cursor.params is not None
    assert cursor.params[5] == topic_monthly_job.PRODUCER_NOOP
    assert cursor.params[6] == topic_monthly_job.ASSIGNMENT_NOT_REQUIRED
    assert cursor.params[6] not in {"pending", "running", "gap"}


def test_run_topic_rpc_extracts_run_id_before_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    """Given code-serving RPC responses, When a run starts, Then status and result are polled by run_id."""
    calls: list[str] = []

    def fake_post(_url: str, payload: dict[str, object], _timeout: int) -> dict[str, object]:
        params = payload["params"]
        assert isinstance(params, dict)
        name = str(params["name"])
        calls.append(name)
        match name:
            case "run_topic_extraction":
                return _mcp_payload({"run_id": "topic_123", "status": "started"})
            case "get_status":
                return _mcp_payload({"run_id": "topic_123", "status": "done"})
            case "get_result":
                return _mcp_payload(
                    {
                        "run_id": "topic_123",
                        "status": "done",
                        "executed_call_count": 88,
                        "db_save_summary": {"stored_run_rows": 1},
                    }
                )
            case unreachable:
                raise AssertionError(f"unexpected RPC: {unreachable}")

    monkeypatch.setattr(topic_monthly_job.time, "sleep", lambda _seconds: None)

    result = topic_monthly_job.run_topic_job(
        topic_monthly_job.JobConfig(json_url="http://code-serving-238:8080/json", poll_interval_seconds=1, max_wait_seconds=3),
        post_json=fake_post,
    )

    assert calls == ["run_topic_extraction", "get_status", "get_result"]
    assert result.run_id == "topic_123"
    assert result.status == "done"
    assert result.executed_call_count == 88


def test_default_config_uses_full_brand_monthly_params(monkeypatch: pytest.MonkeyPatch) -> None:
    """Given no overrides, When config is built, Then monthly runs target full-brand mode."""
    for key in ("TOPIC_MAX_REAL_CALLS", "TOPIC_BRANDS_PER_MARKET", "TOPIC_LARGE_MARKET_LIMIT"):
        monkeypatch.delenv(key, raising=False)

    config = topic_monthly_job._config_from_env()

    assert config.max_real_calls == 350
    assert config.brands_per_market is None
    assert config.large_market_limit == 0


def test_config_rejects_call_cap_above_full_brand_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Given an unsafe call cap, When config is built, Then the job refuses to start."""
    monkeypatch.setenv("TOPIC_MAX_REAL_CALLS", "351")

    with pytest.raises(topic_monthly_job.SchedulerError, match="may not exceed 350"):
        topic_monthly_job._config_from_env()


def _mcp_payload(payload: dict[str, object]) -> dict[str, object]:
    """Return the MCP text-content response shape emitted by code-serving."""
    return {
        "result": {
            "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
            "structuredContent": payload,
        }
    }


class _Connection:
    def __init__(self, cursor: "_Cursor | None" = None) -> None:
        self._cursor = cursor

    def __enter__(self) -> "_Connection":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def cursor(self) -> "_Cursor":
        assert self._cursor is not None
        return self._cursor


class _Cursor:
    def __init__(self) -> None:
        self.params: tuple[object, ...] | None = None

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        return None

    def execute(self, _sql: str, params: tuple[object, ...]) -> None:
        self.params = params
