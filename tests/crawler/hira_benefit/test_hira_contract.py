from __future__ import annotations

from datetime import timedelta

import pytest

from pipeline.scripts.crawler.hira_benefit.contract import (
    ACTIVITY_POLICIES,
    HiraRunMetrics,
    HiraWorkflowInput,
    validate_run_metrics,
)
from pipeline.scripts.crawler.hira_benefit.http_client import HiraRequestPolicy
from pipeline.scripts.crawler.hira_benefit.stage_cli import (
    build_failure_receipt,
    collect_metrics_from_receipt,
    monitored_user_agent,
)


def test_timeout_budget_is_hira_specific_and_has_three_x_margin() -> None:
    config = HiraWorkflowInput(
        run_id="hira-20260725",
        state_root="/tmp/hira-state",
        first_run_mode="date_boundary",
        notice_date_boundary="2023-12-29",
    )

    assert config.expected_seconds <= config.workflow_timeout_seconds / 3
    assert config.workflow_timeout_seconds == 3600
    assert ACTIVITY_POLICIES["collect_details"].start_to_close == timedelta(minutes=30)
    assert config.request_policy == HiraRequestPolicy()


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


def test_date_boundary_first_run_configuration_is_fail_closed() -> None:
    with pytest.raises(ValueError, match="notice_date_boundary"):
        HiraWorkflowInput(
            run_id="hira-20260725",
            state_root="/tmp/hira-state",
            first_run_mode="date_boundary",
        )


def test_row_count_recent_n_mode_is_rejected() -> None:
    with pytest.raises(ValueError, match="date_boundary"):
        HiraWorkflowInput(
            run_id="hira-20260725",
            state_root="/tmp/hira-state",
            first_run_mode="recent_n",
        )


def test_backfill_chunk_requires_manifest_identity_and_index() -> None:
    with pytest.raises(ValueError, match="manifest_sha256"):
        HiraWorkflowInput(
            run_id="hira-backfill-chunk-001",
            state_root="/tmp/hira-state",
            first_run_mode="backfill_all",
            manifest_path="/tmp/manifest.json",
            chunk_index=0,
        )


def test_backfill_chunk_contract_has_no_population_max_notices_gate() -> None:
    config = HiraWorkflowInput(
        run_id="hira-backfill-chunk-001",
        state_root="/tmp/hira-state",
        first_run_mode="backfill_all",
        manifest_path="/tmp/manifest.json",
        manifest_sha256="a" * 64,
        chunk_index=0,
    )

    assert not hasattr(config, "max_notices")


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


def test_circuit_open_failure_receipt_requires_thirty_minute_pause() -> None:
    from pipeline.scripts.crawler.hira_benefit.http_client import CircuitOpenError

    receipt = build_failure_receipt(
        "collect_details",
        CircuitOpenError(reason="http_503", retry_after_seconds=1800),
    )

    assert receipt["status"] == "failed"
    assert receipt["gate_failures"] == ["circuit_open"]
    assert receipt["retry_after_seconds"] == 1800


def test_monitored_user_agent_is_required_for_live_requests() -> None:
    with pytest.raises(RuntimeError, match="HIRA_USER_AGENT"):
        monitored_user_agent(None)
    with pytest.raises(RuntimeError, match="monitored contact"):
        monitored_user_agent(
            "JWHealth-HIRA-InsuranceCriteriaBot/1.0 "
            "(+mailto:<monitored-contact>; approved-internal-sync)"
        )

    assert (
        monitored_user_agent(
            "JWHealth-HIRA-InsuranceCriteriaBot/1.0 "
            "(+mailto:ops@example.com; approved-internal-sync)"
        )
        == "JWHealth-HIRA-InsuranceCriteriaBot/1.0 "
        "(+mailto:ops@example.com; approved-internal-sync)"
    )
