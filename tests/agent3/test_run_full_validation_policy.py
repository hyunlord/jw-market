from __future__ import annotations

from typing import Any

from pipeline.scripts.agent3.run_full import (
    VALIDATION_ISOLATION_ABSOLUTE_LIMIT,
    VALIDATION_ISOLATION_RATE_LIMIT,
    WORKFLOW_ERROR_CONSECUTIVE_LIMIT,
    _isolation_limit_exceeded,
    _run_workflow_with_validation,
    _should_skip_existing,
    _workflow_error_limit_exceeded,
    _workflow_error_summary,
)
from pipeline.scripts.agent3.loader import ExistingAgent3State
from pipeline.scripts.agent3.workflow_client import WorkflowRetryExhaustedError


def _candidate() -> dict[str, Any]:
    return {
        "slice": "IQVIA 성분용량: 0.05%",
        "metric": "recent_growth",
        "value_current": 174892658.0,
        "value_baseline": 119019372.0,
        "delta_abs": 55873286.0,
        "delta_pct": 46.944699052856706,
        "display_numbers": {
            "value_current": "1.7억원",
            "value_baseline": "1.2억원",
            "delta_abs": "55,873,286",
            "delta_pct": "46.9%",
        },
    }


def _summary(narrative: str) -> dict[str, Any]:
    return {
        "brand": "네오세틴",
        "strength_items": [
            {
                "candidate_index": 0,
                "slice": "IQVIA 성분용량: 0.05%",
                "metric": "recent_growth",
                "narrative": narrative,
            }
        ],
        "limitations": [],
    }


class FakeClient:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.calls = 0

    def run(self, payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        response = self.responses[self.calls]
        self.calls += 1
        return response, {"call": self.calls}


def test_validation_retry_uses_second_valid_summary() -> None:
    client = FakeClient(
        [
            _summary("46.944% 성장했습니다."),
            _summary("성분용량 구간에서 46.9% 성장했습니다."),
        ]
    )

    result = _run_workflow_with_validation(
        client=client,
        profile={"brand": "네오세틴"},
        candidates=[_candidate()],
        brand="네오세틴",
    )

    assert result.status == "ready"
    assert result.workflow_calls == 2
    assert result.validation_retried == 1
    assert result.validation_isolated == 0
    assert result.summary["strength_items"][0]["numbers"]["delta_pct"] == 46.944699052856706


def test_validation_failure_after_retry_is_profile_only_isolation() -> None:
    client = FakeClient(
        [
            _summary("46.944% 성장했습니다."),
            _summary("46.944% 증가했습니다."),
        ]
    )

    result = _run_workflow_with_validation(
        client=client,
        profile={"brand": "네오세틴"},
        candidates=[_candidate()],
        brand="네오세틴",
    )

    assert result.status == "validation_isolated"
    assert result.workflow_calls == 2
    assert result.validation_retried == 1
    assert result.validation_isolated == 1
    assert result.summary["strength_items"] == []
    assert result.summary["unavailable_reason"] == "validation_failed"
    assert "46.944%" in result.isolation_log[0]["retry_errors"][0]


def test_isolation_limit_triggers_on_rate_or_absolute_limit() -> None:
    assert _isolation_limit_exceeded(isolated=1, workflow_targets=27) is False
    assert _isolation_limit_exceeded(isolated=3, workflow_targets=100) is True
    assert _isolation_limit_exceeded(
        isolated=VALIDATION_ISOLATION_ABSOLUTE_LIMIT,
        workflow_targets=1000,
    ) is True
    assert _isolation_limit_exceeded(
        isolated=int(VALIDATION_ISOLATION_RATE_LIMIT * 100),
        workflow_targets=100,
    ) is False


def test_validation_failed_existing_row_is_not_skipped() -> None:
    existing = ExistingAgent3State(input_hash="same", workflow_rev=5365, validation_failed=True)

    assert _should_skip_existing(existing, input_hash="same", workflow_rev=5365) is False
    assert (
        _should_skip_existing(
            ExistingAgent3State(input_hash="same", workflow_rev=5365, validation_failed=False),
            input_hash="same",
            workflow_rev=5365,
        )
        is True
    )


def test_workflow_error_summary_is_profile_only_and_retriable() -> None:
    summary = _workflow_error_summary(
        "대웅 몬테루카스트",
        {"brand": "대웅 몬테루카스트"},
        [_candidate()],
        WorkflowRetryExhaustedError("failed", attempts=4, last_error="HTTP 500"),
    )

    assert summary["strength_items"] == []
    assert summary["unavailable_reason"] == "workflow_error"
    assert summary["workflow_error"]["attempts"] == 4
    assert _should_skip_existing(
        ExistingAgent3State(input_hash="same", workflow_rev=5365, validation_failed=True),
        input_hash="same",
        workflow_rev=5365,
    ) is False


def test_workflow_error_service_down_guard_triggers_on_three_consecutive_brands() -> None:
    assert _workflow_error_limit_exceeded(WORKFLOW_ERROR_CONSECUTIVE_LIMIT - 1) is False
    assert _workflow_error_limit_exceeded(WORKFLOW_ERROR_CONSECUTIVE_LIMIT) is True
