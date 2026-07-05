from __future__ import annotations

from typing import Any

from pipeline.scripts.agent3.run_full import (
    VALIDATION_ISOLATION_ABSOLUTE_LIMIT,
    VALIDATION_ISOLATION_RATE_LIMIT,
    _isolation_limit_exceeded,
    _run_workflow_with_validation,
)


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
            _summary("0.05% 용량에서 46.9% 성장했습니다."),
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
            _summary("0.05% 용량에서 46.9% 성장했습니다."),
            _summary("0.05% 성분용량 매출이 46.9% 증가했습니다."),
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
    assert "0.05%" in result.isolation_log[0]["retry_errors"][0]


def test_isolation_limit_triggers_on_rate_or_absolute_limit() -> None:
    assert _isolation_limit_exceeded(isolated=1, workflow_targets=100) is False
    assert _isolation_limit_exceeded(isolated=3, workflow_targets=100) is True
    assert _isolation_limit_exceeded(
        isolated=VALIDATION_ISOLATION_ABSOLUTE_LIMIT + 1,
        workflow_targets=1000,
    ) is True
    assert _isolation_limit_exceeded(
        isolated=int(VALIDATION_ISOLATION_RATE_LIMIT * 100),
        workflow_targets=100,
    ) is False
