from __future__ import annotations

import json

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
    assert config.brands_per_market == 10000
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
