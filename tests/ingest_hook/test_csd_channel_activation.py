from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from pipeline.scripts.ingest_hook import csd_channel_activation as activation


def _plan() -> activation.ActivationPlan:
    return activation.plan_for_run(
        "20260807010203000000",
        created_at=datetime(2026, 8, 7, 1, 2, 3, tzinfo=timezone.utc),
    )


def test_plan_uses_wrapper_derived_candidate_and_rollback_names() -> None:
    plan = _plan()

    assert plan.raw.live == activation.TableRef(
        "jw_brand_activity_raw_stage", "raw_csd_channel_dynamics"
    )
    assert plan.stage.live == activation.TableRef(
        "jw_brand_activity_stage", "csd_channel_dynamics_stage"
    )
    assert plan.raw.candidate.schema == "jw_brand_activity_raw_stage_csd_20260807010203000000"
    assert plan.stage.candidate.schema == "jw_brand_activity_stage_csd_20260807010203000000"
    assert plan.raw.candidate.table == plan.raw.live.table
    assert plan.stage.candidate.table == plan.stage.live.table
    assert plan.raw.rollback == activation.TableRef(
        "jw_csd_channel_rollback_raw",
        "raw_csd_channel_dynamics__rollback_20260807010203000000",
    )
    assert plan.stage.rollback == activation.TableRef(
        "jw_csd_channel_rollback_stage",
        "csd_channel_dynamics_stage__rollback_20260807010203000000",
    )
    assert all(len(ref.table) <= 64 for ref in plan.table_refs())


def test_copy_batches_never_exceed_one_thousand_rows() -> None:
    sizes = [len(batch) for batch in activation.batches(list(range(324_885)))]
    stage_sizes = [len(batch) for batch in activation.batches(list(range(49_894)))]

    assert max(sizes) == max(stage_sizes) == 1000
    assert len(sizes) == 325
    assert len(stage_sizes) == 50
    assert len(sizes) + len(stage_sizes) == 375


def test_uploaded_rows_are_read_from_workbooks_not_selected_from_staging(
    monkeypatch,
) -> None:
    row = object()
    monkeypatch.setattr(activation, "source_sha256", lambda path: f"sha:{path.name}")
    monkeypatch.setattr(
        activation,
        "read_csd_source_rows",
        lambda path, digest: [row] if digest == f"sha:{path.name}" else [],
    )
    monkeypatch.setattr(activation, "csd_raw_record", lambda value: {"row": value})

    assert list(activation._uploaded_batches((Path("a.xlsx"),))) == [[{"row": row}]]


def test_period_gate_accepts_boundary_partial_quarters_only() -> None:
    periods = activation.month_range("2023-06", "2026-05")

    result = activation.validate_period_contract(periods)

    assert result.months == 36
    assert result.complete_quarters[0] == "2023-Q3"
    assert result.complete_quarters[-1] == "2026-Q1"
    assert result.excluded_boundary_months == ("2023-06", "2026-04", "2026-05")


def test_period_gate_rejects_missing_internal_month() -> None:
    periods = list(activation.month_range("2023-06", "2026-05"))
    periods.remove("2024-02")

    with pytest.raises(activation.CandidateValidationError, match="continuous 36 months"):
        activation.validate_period_contract(periods)


def test_period_gate_rejects_empty_stage_with_contract_error() -> None:
    with pytest.raises(
        activation.CandidateValidationError,
        match="continuous 36 months",
    ):
        activation.validate_period_contract(())


def test_plan_scope_accepts_only_the_expected_live_pair() -> None:
    plan = _plan()

    activation.validate_plan_scope(
        plan,
        expected_run_id=plan.run_id,
        raw_schema="jw_brand_activity_raw_stage",
        stage_schema="jw_brand_activity_stage",
    )


def test_plan_rejects_noncanonical_scope_not_supported_by_fixed_wrapper() -> None:
    with pytest.raises(ValueError, match="canonical live scope"):
        activation.plan_for_run(
            "20260807010203000000",
            raw_schema="jw_brand_activity_raw_stage_shadow_probe",
            stage_schema="jw_brand_activity_stage_shadow_probe",
        )


def test_plan_scope_rejects_payload_redirect_to_another_table() -> None:
    plan = _plan()
    redirected = activation.ActivationPlan(
        run_id=plan.run_id,
        raw=activation.TablePair(
            activation.TableRef(plan.raw.live.schema, "another_table"),
            plan.raw.candidate,
            plan.raw.rollback,
        ),
        stage=plan.stage,
    )

    with pytest.raises(activation.CandidateValidationError, match="live table scope"):
        activation.validate_plan_scope(
            redirected,
            expected_run_id=plan.run_id,
            raw_schema="jw_brand_activity_raw_stage",
            stage_schema="jw_brand_activity_stage",
        )


def test_plan_scope_rejects_candidate_not_derived_from_run() -> None:
    plan = _plan()
    redirected = activation.ActivationPlan(
        run_id=plan.run_id,
        raw=activation.TablePair(
            plan.raw.live,
            activation.TableRef("unrelated_schema", plan.raw.candidate.table),
            plan.raw.rollback,
        ),
        stage=plan.stage,
    )

    with pytest.raises(activation.CandidateValidationError, match="candidate scope"):
        activation.validate_plan_scope(
            redirected,
            expected_run_id=plan.run_id,
            raw_schema="jw_brand_activity_raw_stage",
            stage_schema="jw_brand_activity_stage",
        )


def test_plan_scope_rejects_lookalike_rollback_table() -> None:
    plan = _plan()
    redirected = activation.ActivationPlan(
        run_id=plan.run_id,
        raw=activation.TablePair(
            plan.raw.live,
            plan.raw.candidate,
            activation.TableRef(
                plan.raw.rollback.schema,
                plan.raw.rollback.table.replace("__rollback_", "_other__rollback_"),
            ),
        ),
        stage=plan.stage,
    )

    with pytest.raises(activation.CandidateValidationError, match="rollback scope"):
        activation.validate_plan_scope(
            redirected,
            expected_run_id=plan.run_id,
            raw_schema="jw_brand_activity_raw_stage",
            stage_schema="jw_brand_activity_stage",
        )


def test_channel_gate_checks_total_and_combined_channel() -> None:
    rows = (
        activation.ChannelAggregate("2026-01", "M", "B", "JW", "TOTAL", 10),
        activation.ChannelAggregate("2026-01", "M", "B", "JW", "GH", 3),
        activation.ChannelAggregate("2026-01", "M", "B", "JW", "SHPPI", 2),
        activation.ChannelAggregate("2026-01", "M", "B", "JW", "CPPI", 5),
        activation.ChannelAggregate("2026-01", "M", "B", "JW", "GH+SHPPI", 5),
    )

    assert activation.validate_channel_totals(rows).groups_checked == 1


def test_channel_gate_rejects_mismatch() -> None:
    rows = (
        activation.ChannelAggregate("2026-01", "M", "B", "JW", "TOTAL", 11),
        activation.ChannelAggregate("2026-01", "M", "B", "JW", "GH", 3),
        activation.ChannelAggregate("2026-01", "M", "B", "JW", "SHPPI", 2),
        activation.ChannelAggregate("2026-01", "M", "B", "JW", "CPPI", 5),
        activation.ChannelAggregate("2026-01", "M", "B", "JW", "GH+SHPPI", 5),
    )

    with pytest.raises(activation.CandidateValidationError, match="TOTAL"):
        activation.validate_channel_totals(rows)


def test_commissioning_observes_stage_without_enforcing_post_gate() -> None:
    rows = (
        activation.CsdRow(
            source_file="ChannelDynamics Oct. 25.xlsx",
            source_sheet="Data",
            source_row_no=2,
            period_ym="2025-10",
            market="M",
            jw_channel="TOTAL",
            master_product="B",
            representing_company="JW",
            product_details=1,
        ),
    )

    periods, channels = activation.stage_gate_evidence(rows, enforce=False)

    assert periods == activation.PeriodContract(1, (), ())
    assert channels == activation.ChannelGateResult(0)


def test_production_still_enforces_channel_post_gate() -> None:
    rows = (
        activation.CsdRow(
            source_file="ChannelDynamics Oct. 25.xlsx",
            source_sheet="Data",
            source_row_no=2,
            period_ym="2025-10",
            market="M",
            jw_channel="TOTAL",
            master_product="B",
            representing_company="JW",
            product_details=1,
        ),
    )

    with pytest.raises(activation.CandidateValidationError, match="continuous 36 months"):
        activation.stage_gate_evidence(rows, enforce=True)


class _RecordingCursor:
    def __init__(self, responses: list[list[dict[str, object]]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.current: list[dict[str, object]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> None:
        self.calls.append((sql, params))
        self.current = self.responses.pop(0)

    def fetchall(self):
        return self.current

    def fetchone(self):
        return self.current[0] if self.current else None


class _RecordingConnection:
    def __init__(self, responses: list[list[dict[str, object]]]) -> None:
        self.recording_cursor = _RecordingCursor(responses)

    def cursor(self):
        return self.recording_cursor


def _evidence() -> activation.CandidateEvidence:
    return activation.CandidateEvidence(
        raw=activation.TableFingerprint(20, 21, 22),
        stage=activation.TableFingerprint(18, 19, 20),
        live_raw=activation.TableFingerprint(10, 11, 12),
        live_stage=activation.TableFingerprint(8, 9, 10),
        periods=activation.PeriodContract(36, (), ()),
        channels=activation.ChannelGateResult(1),
        commits=375,
    )


@pytest.mark.parametrize("state", ["applied", "applied_observed"])
def test_publish_calls_wrapper_once_with_candidate_and_expected_live_fingerprints(
    state: str,
) -> None:
    connection = _RecordingConnection([[{"publish_state": state}]])

    assert activation.publish_candidate(connection, _plan(), _evidence()) is activation.SwapVerdict.APPLIED
    assert connection.recording_cursor.calls == [
        (
            "CALL `jw_csd_channel_control`.`csd_atomic_publish`(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                "20260807010203000000",
                20,
                21,
                22,
                18,
                19,
                20,
                10,
                11,
                12,
                8,
                9,
                10,
            ),
        )
    ]


def test_abandon_calls_only_the_wrapper_with_explicit_confirmation() -> None:
    connection = _RecordingConnection([[{"abandon_state": "removed"}]])

    activation.abandon_candidate(connection, _plan())

    assert connection.recording_cursor.calls == [
        (
            "CALL `jw_csd_channel_control`.`csd_candidate_abandon`(%s,%s)",
            ("20260807010203000000", "ABANDON_UNPUBLISHED_CANDIDATE"),
        )
    ]
