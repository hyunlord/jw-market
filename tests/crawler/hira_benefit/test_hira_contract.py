from __future__ import annotations

from datetime import timedelta

import pytest

from pipeline.scripts.crawler.hira_benefit.contract import (
    ACTIVITY_POLICIES,
    PAGES_PER_BATCH,
    HiraRunMetrics,
    HiraWorkflowInput,
    page_batches,
    scheduled_workflow_input,
    stage_receipt_name,
    validate_run_metrics,
)
from pipeline.scripts.crawler.hira_benefit.http_client import HiraRequestPolicy
from pipeline.scripts.crawler.hira_benefit.stage_cli import (
    build_failure_receipt,
    collect_metrics_from_receipt,
    monitored_user_agent,
)


def _scheduled() -> HiraWorkflowInput:
    return scheduled_workflow_input(
        state_root="/tmp/hira-state",
        notice_date_boundary="2023-12-29",
    )


def test_timeout_budget_is_hira_specific_and_has_three_x_margin() -> None:
    config = _scheduled()

    assert config.expected_seconds <= config.workflow_timeout_seconds / 3
    assert config.workflow_timeout_seconds == 3600
    assert ACTIVITY_POLICIES["collect_details"].start_to_close == timedelta(minutes=30)
    assert config.request_policy == HiraRequestPolicy()


def test_budget_gate_models_index_enumeration_not_just_chunk_size() -> None:
    """The gate that let a 153-page enumeration through must now reject it.

    Before the split the model only priced ``chunk_size`` detail fetches, so a
    full-index enumeration cost exactly zero seconds in the gate's view.
    """

    config = _scheduled()

    assert config.enumeration_page_count == 160
    assert config.discovery_expected_seconds > 800
    # Enumeration dominates: dropping it would understate the run by >2x.
    assert (
        config.discovery_expected_seconds > config.detail_expected_seconds
    )

    with pytest.raises(ValueError, match="3x expected margin"):
        # The pre-split production shape: full enumeration budgeted alongside
        # the 500-notice chunk ceiling.
        HiraWorkflowInput(
            run_id="hira-20260725",
            state_root="/tmp/hira-state",
            first_run_mode="date_boundary",
            notice_date_boundary="2023-12-29",
            expected_index_pages=153,
        )


def test_backfill_chunk_pays_no_enumeration_cost() -> None:
    """A manifest chunk reads its manifest; it never walks the index."""

    config = HiraWorkflowInput(
        run_id="hira-backfill-chunk-001",
        state_root="/tmp/hira-state",
        first_run_mode="backfill_all",
        manifest_path="/tmp/manifest.json",
        manifest_sha256="a" * 64,
        chunk_index=0,
    )

    assert config.enumeration_page_count == 0
    assert config.enumeration_batch_count == 0
    assert config.expected_detail_count == config.chunk_size
    assert config.expected_seconds * 3 <= config.workflow_timeout_seconds


def test_page_batch_budget_keeps_three_x_margin_over_paced_enumeration() -> None:
    config = _scheduled()
    budget = ACTIVITY_POLICIES["discover_page_batch"].start_to_close.total_seconds()

    assert config.pages_per_batch == PAGES_PER_BATCH == 18
    assert config.page_batch_worst_seconds * 3 <= budget
    # One more page per batch would break the margin: 18 is the derived ceiling.
    over = config.pages_per_batch + 2
    assert (over * config.list_request_worst_seconds + 10.0) * 3 > budget


def test_page_batch_budget_is_invariant_as_the_index_grows() -> None:
    """Growth must add batches, never enlarge one batch's budget."""

    small = page_batches(153, PAGES_PER_BATCH)
    large = page_batches(400, PAGES_PER_BATCH)

    assert len(large) > len(small)
    assert all(end - start + 1 <= PAGES_PER_BATCH for start, end in small + large)
    # Page 1 belongs to the probe; batches cover 2..N with no gap or overlap.
    assert small[0][0] == 2
    assert small[-1][1] == 153
    assert [start for start, _ in small[1:]] == [end + 1 for _, end in small[:-1]]


def test_oversized_page_batch_is_rejected_by_the_contract() -> None:
    with pytest.raises(ValueError, match="3x margin over paced enumeration"):
        HiraWorkflowInput(
            run_id="hira-20260725",
            state_root="/tmp/hira-state",
            first_run_mode="date_boundary",
            notice_date_boundary="2023-12-29",
            expected_detail_notices=120,
            pages_per_batch=40,
        )


def test_workflow_budget_accounting_is_explicit_about_both_models() -> None:
    """Pin the two budget models so §4's numbers stay test-backed.

    Model A (expected) is what the gate contracts and what the run costs when
    nothing times out. Model B (every attempt exhausts its StartToClose) does not
    fit 3600s — and never did: ``collect_details`` alone already exceeded the
    workflow timeout before enumeration was split. Model B is bounded by the
    workflow execution timeout, which kills the run without advancing state.
    """

    config = _scheduled()
    backoff = 15.0

    def worst(stage: str, count: int = 1) -> float:
        policy = ACTIVITY_POLICIES[stage]
        per_attempt = policy.start_to_close.total_seconds()
        attempts = policy.maximum_attempts
        return count * (per_attempt * attempts + backoff * (attempts - 1))

    model_a = config.expected_seconds
    model_b = (
        worst("discover_probe")
        + worst("discover_page_batch", config.enumeration_batch_count)
        + worst("discover_reduce")
        + worst("collect_details")
        + worst("persist_results")
        + worst("verify_run")
    )

    assert config.enumeration_batch_count == 9
    assert model_a * 3 <= config.workflow_timeout_seconds
    assert 3400 <= model_a * 3 <= 3500
    assert model_b > config.workflow_timeout_seconds
    # The pre-existing stage alone already blew the same ceiling, so Model B is
    # not a regression introduced by the split.
    assert worst("collect_details") > config.workflow_timeout_seconds


def test_page_batch_receipts_are_not_shared_between_batches() -> None:
    first = stage_receipt_name("discover_page_batch", page_start=2, page_end=19)
    second = stage_receipt_name("discover_page_batch", page_start=20, page_end=37)

    assert first != second
    assert stage_receipt_name("discover_reduce") == "discover_reduce"
    with pytest.raises(ValueError, match="page range"):
        stage_receipt_name("discover_page_batch")


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
