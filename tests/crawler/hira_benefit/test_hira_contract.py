from __future__ import annotations

from datetime import timedelta

import pytest

from pipeline.scripts.crawler.hira_benefit.contract import (
    ACTIVITY_POLICIES,
    HiraRunMetrics,
    HiraWorkflowInput,
    validate_run_metrics,
)
from pipeline.scripts.crawler.hira_benefit.stage_cli import (
    collect_metrics_from_receipt,
)


def test_timeout_budget_is_hira_specific_and_has_three_x_margin() -> None:
    config = HiraWorkflowInput(
        run_id="hira-20260725",
        state_root="/tmp/hira-state",
        first_run_mode="recent_n",
        recent_limit=500,
    )

    assert config.expected_seconds <= config.workflow_timeout_seconds / 3
    assert config.workflow_timeout_seconds == 3600
    assert ACTIVITY_POLICIES["collect_details"].start_to_close == timedelta(minutes=30)


def test_four_condition_gate_passes_without_unapproved_threshold() -> None:
    result = validate_run_metrics(
        HiraRunMetrics(
            exit_code=0,
            failures=0,
            identity_gap=0,
            pending_gap=0,
            parsed_count=20,
            partial_count=3,
            failed_count=1,
        )
    )

    assert result.passed is True
    assert result.alerts == ()


def test_failed_ratio_threshold_is_parameterized() -> None:
    result = validate_run_metrics(
        HiraRunMetrics(0, 0, 0, 0, 20, 3, 2),
        failed_alert_ratio=0.05,
    )

    assert result.alerts == ("parse_failed_ratio=0.1000 threshold=0.0500",)


@pytest.mark.parametrize("field", ["exit_code", "failures", "identity_gap", "pending_gap"])
def test_each_success_gate_fails_closed(field: str) -> None:
    values = {
        "exit_code": 0,
        "failures": 0,
        "identity_gap": 0,
        "pending_gap": 0,
        "parsed_count": 1,
        "partial_count": 0,
        "failed_count": 0,
    }
    values[field] = 1

    result = validate_run_metrics(HiraRunMetrics(**values))

    assert result.passed is False
    assert field in result.failures


def test_first_run_configuration_is_fail_closed() -> None:
    with pytest.raises(ValueError, match="recent_limit"):
        HiraWorkflowInput(
            run_id="hira-20260725",
            state_root="/tmp/hira-state",
            first_run_mode="recent_n",
        )


def test_collect_metrics_rejects_failed_receipt_before_persist() -> None:
    with pytest.raises(RuntimeError, match="collect receipt is not complete"):
        collect_metrics_from_receipt(
            {
                "status": "failed",
                "exit_code": 1,
                "failures": 1,
                "identity_gap": 1,
                "pending_gap": 1,
                "parsed_count": 9,
                "partial_count": 0,
                "failed_count": 0,
            }
        )
