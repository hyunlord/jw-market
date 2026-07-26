from __future__ import annotations

from dataclasses import replace

import pytest

from pipeline.scripts.analysis.brand_activity.auto_topic import topic_assignment_handoff as handoff
from pipeline.scripts.analysis.brand_activity.auto_topic import topic_assignment_handoff_db as handoff_db
from pipeline.scripts.analysis.brand_activity.auto_topic.row_topic_assignment import AssignmentInputRow


def _scope(scope_id: str, *, source_row_count: int = 2) -> handoff.TopicScopeSnapshot:
    return handoff.TopicScopeSnapshot(
        scope_id=scope_id,
        display_name=scope_id,
        atc4_values=(scope_id.rsplit(":", 1)[-1],),
        quality_grade="A",
        source_row_count=source_row_count,
        payload={"axis": {"topics": [{"topic_id": "T1"}]}},
    )


def _row(row_id: int, scope_id: str = "atc4:A02B2") -> AssignmentInputRow:
    return AssignmentInputRow(
        row_id=row_id,
        scope_id=scope_id,
        brand="BRAND",
        keyword_text=f"message {row_id}",
        stage_row_sha256=f"hash-{row_id}",
    )


def _receipt(
    *,
    axis_status: str = handoff.AXIS_COMPLETE,
    assignment_status: str = handoff.ASSIGNMENT_PENDING,
) -> handoff.AssignmentHandoffReceipt:
    population = handoff.population_identity((_row(1), _row(2)))
    scopes = handoff.scope_identity((_scope("atc4:A02B2"),))
    return handoff.AssignmentHandoffReceipt(
        run_id="topic-run",
        target_mode="strategic",
        input_fingerprint="f" * 64,
        expected_scope_count=scopes.count,
        stored_scope_count=scopes.count,
        scope_identity_sha256=scopes.sha256,
        assignment_population_count=population.count,
        assignment_population_sha256=population.sha256,
        axis_status=axis_status,
        assignment_status=assignment_status,
    )


def test_axis_failure_without_complete_receipt_blocks_assignment() -> None:
    """Given axis generation failed, assignment must not run without a complete receipt."""
    with pytest.raises(handoff.HandoffBlockedError, match="receipt is missing"):
        handoff.require_assignment_ready(None, (_row(1),))

    with pytest.raises(handoff.HandoffBlockedError, match="axis_status=incomplete"):
        handoff.require_assignment_ready(
            _receipt(axis_status=handoff.AXIS_INCOMPLETE),
            (_row(1), _row(2)),
        )


def test_partial_axis_completion_blocks_the_whole_run() -> None:
    """Given only one of two scopes persisted, no completed scope is dispatched alone."""
    expected = handoff.scope_identity(
        (_scope("atc4:A02B2"), _scope("atc4:C10A1"))
    )
    stored = handoff.scope_identity((_scope("atc4:A02B2"),))

    completion = handoff.evaluate_axis_completion(expected, stored)

    assert completion.axis_status == handoff.AXIS_INCOMPLETE
    assert completion.assignment_status == handoff.ASSIGNMENT_BLOCKED
    assert completion.whole_run_eligible is False
    assert completion.expected_scope_count == 2
    assert completion.stored_scope_count == 1


def test_reconciliation_finds_an_artificially_pending_receipt() -> None:
    """Given a durable pending receipt, reconciliation must select its exact run id."""
    receipts = (
        replace(_receipt(), run_id="pending-run"),
        replace(
            _receipt(),
            run_id="complete-run",
            assignment_status=handoff.ASSIGNMENT_COMPLETE,
        ),
        replace(
            _receipt(),
            run_id="blocked-run",
            axis_status=handoff.AXIS_INCOMPLETE,
            assignment_status=handoff.ASSIGNMENT_BLOCKED,
        ),
    )

    assert handoff.reconcilable_run_ids(receipts) == ("pending-run",)


def test_gap_detection_reports_missing_hash_and_zero_assignment_scope() -> None:
    """Given incomplete status evidence, the exact missing and zero-assignment gaps are visible."""
    expected_rows = (
        _row(1),
        _row(2),
        _row(3, scope_id="atc4:C10A1"),
    )
    status_rows = (
        handoff.AssignmentStatusSnapshot(
            scope_id="atc4:A02B2",
            row_id=1,
            stage_row_sha256="hash-1",
            assignment_count=1,
        ),
        handoff.AssignmentStatusSnapshot(
            scope_id="atc4:A02B2",
            row_id=2,
            stage_row_sha256="stale-hash",
            assignment_count=0,
        ),
        handoff.AssignmentStatusSnapshot(
            scope_id="atc4:C10A1",
            row_id=3,
            stage_row_sha256="hash-3",
            assignment_count=0,
        ),
    )

    gap = handoff.evaluate_assignment_gap(
        expected_rows,
        status_rows,
        assignment_scope_counts={"atc4:A02B2": 1, "atc4:C10A1": 0},
    )

    assert gap.complete is False
    assert gap.missing_row_ids == (2,)
    assert gap.hash_mismatch_row_ids == (2,)
    assert gap.zero_assignment_scope_ids == ("atc4:C10A1",)


def test_scope_and_population_identity_are_deterministic_across_three_orders() -> None:
    """Given the same exact population in different orders, identity is stable."""
    scopes = (_scope("atc4:A02B2"), _scope("atc4:C10A1"))
    rows = (_row(1), _row(2), _row(3, scope_id="atc4:C10A1"))

    scope_hashes = {
        handoff.scope_identity(order).sha256
        for order in (scopes, tuple(reversed(scopes)), scopes)
    }
    population_hashes = {
        handoff.population_identity(order).sha256
        for order in (rows, tuple(reversed(rows)), rows)
    }

    assert len(scope_hashes) == 1
    assert len(population_hashes) == 1


class _Connection:
    def __init__(self) -> None:
        self.commits = 0

    def commit(self) -> None:
        self.commits += 1


def test_db_axis_partial_completion_writes_blocked_receipt_without_loading_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Given one stored scope is missing, the DB handoff blocks the whole run."""
    stored_receipts: list[handoff.AssignmentHandoffReceipt] = []
    monkeypatch.setattr(
        handoff_db,
        "_stored_scope_snapshots",
        lambda *_args, **_kwargs: (_scope("atc4:A02B2"),),
    )
    monkeypatch.setattr(
        handoff_db,
        "load_assignment_rows",
        lambda *_args, **_kwargs: pytest.fail("partial axis must not load assignment rows"),
    )
    monkeypatch.setattr(
        handoff_db,
        "_upsert_receipt",
        lambda _connection, **kwargs: stored_receipts.append(kwargs["receipt"]),
    )

    receipt = handoff_db.record_axis_handoff(
        _Connection(),
        schema="jw_brand_activity_stage",
        handoff_table="mart_brand_activity_assignment_handoff",
        topics_table="mart_brand_activity_topics",
        run_id="topic-run",
        target_mode="strategic",
        input_fingerprint="f" * 64,
        expected_scopes=(
            _scope("atc4:A02B2"),
            _scope("atc4:C10A1"),
        ),
    )

    assert receipt.axis_status == handoff.AXIS_INCOMPLETE
    assert receipt.assignment_status == handoff.ASSIGNMENT_BLOCKED
    assert stored_receipts == [receipt]


def test_db_axis_exact_completion_records_population_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Given exact stored scopes, the receipt pins the stage-row population."""
    scope = _scope("atc4:A02B2")
    stored_receipts: list[handoff.AssignmentHandoffReceipt] = []
    monkeypatch.setattr(
        handoff_db,
        "_stored_scope_snapshots",
        lambda *_args, **_kwargs: (scope,),
    )
    monkeypatch.setattr(
        handoff_db,
        "load_scope_rubrics",
        lambda *_args, **_kwargs: ["rubric"],
    )
    monkeypatch.setattr(
        handoff_db,
        "load_assignment_rows",
        lambda *_args, **_kwargs: [_row(1), _row(2)],
    )
    monkeypatch.setattr(
        handoff_db,
        "_upsert_receipt",
        lambda _connection, **kwargs: stored_receipts.append(kwargs["receipt"]),
    )

    receipt = handoff_db.record_axis_handoff(
        _Connection(),
        schema="jw_brand_activity_stage",
        handoff_table="mart_brand_activity_assignment_handoff",
        topics_table="mart_brand_activity_topics",
        run_id="topic-run",
        target_mode="strategic",
        input_fingerprint="f" * 64,
        expected_scopes=(scope,),
    )

    assert receipt.axis_status == handoff.AXIS_COMPLETE
    assert receipt.assignment_status == handoff.ASSIGNMENT_PENDING
    assert receipt.assignment_population_count == 2
    assert stored_receipts == [receipt]


def test_exact_axis_completion_allows_only_the_whole_run() -> None:
    """Given exact scope identity, eligibility applies to the whole run."""
    identity = handoff.scope_identity(
        (_scope("atc4:A02B2"), _scope("atc4:C10A1"))
    )

    completion = handoff.evaluate_axis_completion(identity, identity)

    assert completion.whole_run_eligible is True
    assert completion.assignment_status == handoff.ASSIGNMENT_PENDING


def test_handoff_ddl_and_pending_query_are_fail_closed() -> None:
    """Given the durable table contract, only exact completed axes are reconcilable."""
    ddl = handoff_db.handoff_table_ddl("jw_brand_activity_stage", "handoff")
    query = handoff_db.pending_handoff_query("jw_brand_activity_stage", "handoff")

    assert "PRIMARY KEY (run_id)" in ddl
    assert "scope_identity_sha256 CHAR(64) NOT NULL" in ddl
    assert "assignment_population_sha256 CHAR(64) NOT NULL" in ddl
    assert "axis_status='complete'" in query
    assert "assignment_status IN ('pending','running','gap')" in query
