from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.scripts.analysis.brand_activity.auto_topic import row_topic_assignment as rta
from pipeline.scripts.analysis.brand_activity.auto_topic import row_topic_runner
from pipeline.scripts.analysis.brand_activity.auto_topic import row_topic_sql


def _row(row_id: int, brand: str = "THRUPAS", period: str = "2026-05", specialty: str = "Urologists") -> rta.AssignmentInputRow:
    return rta.AssignmentInputRow(
        row_id=row_id,
        scope_id="atc4:G04C2",
        brand=brand,
        keyword_text=f"message {row_id}",
        period_ym=period,
        visit_location="HOSPITAL",
        specialty=specialty,
        interest="VERY USEFUL",
        prescription_evolution="increase",
    )


def test_share_definition_counts_each_topic_independently_when_rows_have_multiple_topics() -> None:
    """Given multi-topic row assignments, When shares are aggregated, Then totals may exceed 100%."""
    rows = [_row(1), _row(2), _row(3), _row(4)]
    assignments = [
        rta.RowTopicAssignment(row_id=1, scope_id="atc4:G04C2", brand="THRUPAS", topic_id="T1", topic_set_version="v1", prompt_version="row_topic_v1", batch_id="b1"),
        rta.RowTopicAssignment(row_id=1, scope_id="atc4:G04C2", brand="THRUPAS", topic_id="T2", topic_set_version="v1", prompt_version="row_topic_v1", batch_id="b1"),
        rta.RowTopicAssignment(row_id=2, scope_id="atc4:G04C2", brand="THRUPAS", topic_id="T1", topic_set_version="v1", prompt_version="row_topic_v1", batch_id="b1"),
        rta.RowTopicAssignment(row_id=3, scope_id="atc4:G04C2", brand="THRUPAS", topic_id="T2", topic_set_version="v1", prompt_version="row_topic_v1", batch_id="b1"),
    ]

    shares = rta.aggregate_topic_shares(rows, assignments)

    assert [share.as_payload() for share in shares] == [
        {"topic_id": "T1", "label": "", "affected_row_count": 2, "share_pct": 50.0},
        {"topic_id": "T2", "label": "", "affected_row_count": 2, "share_pct": 50.0},
    ]
    assert sum(share.share_pct for share in shares) == 100.0


def test_share_definition_allows_sum_above_100_for_independent_topic_yes_no() -> None:
    """Given overlapping topic assignments, When shares are aggregated, Then the sum is not normalized."""
    rows = [_row(1), _row(2)]
    assignments = [
        rta.RowTopicAssignment(row_id=1, scope_id="atc4:G04C2", brand="THRUPAS", topic_id="T1", topic_set_version="v1", prompt_version="row_topic_v1", batch_id="b1"),
        rta.RowTopicAssignment(row_id=1, scope_id="atc4:G04C2", brand="THRUPAS", topic_id="T2", topic_set_version="v1", prompt_version="row_topic_v1", batch_id="b1"),
        rta.RowTopicAssignment(row_id=2, scope_id="atc4:G04C2", brand="THRUPAS", topic_id="T1", topic_set_version="v1", prompt_version="row_topic_v1", batch_id="b1"),
        rta.RowTopicAssignment(row_id=2, scope_id="atc4:G04C2", brand="THRUPAS", topic_id="T2", topic_set_version="v1", prompt_version="row_topic_v1", batch_id="b1"),
    ]

    shares = rta.aggregate_topic_shares(rows, assignments)

    assert [share.share_pct for share in shares] == [100.0, 100.0]
    assert sum(share.share_pct for share in shares) == 200.0


def test_parse_assignment_response_rejects_missing_duplicate_and_unknown_topics() -> None:
    """Given malformed model JSON, When parsed, Then exact id echo and topic ids are enforced."""
    batch = [_row(1), _row(2)]
    topics = {"T1", "T2"}

    with pytest.raises(rta.AssignmentParseError, match="missing"):
        rta.parse_assignment_response('{"assignments":[{"row_id":1,"topics":["T1"]}]}', batch, topics, "v1", "b1")

    with pytest.raises(rta.AssignmentParseError, match="duplicate"):
        rta.parse_assignment_response('{"assignments":[{"row_id":1,"topics":["T1"]},{"row_id":1,"topics":[]}]}', batch, topics, "v1", "b1")

    with pytest.raises(rta.AssignmentParseError, match="unknown topic"):
        rta.parse_assignment_response('{"assignments":[{"row_id":1,"topics":["NOPE"]},{"row_id":2,"topics":[]}]}', batch, topics, "v1", "b1")


def test_filter_distribution_reuses_assignments_without_llm_calls() -> None:
    """Given row assignments, When a specialty filter is applied, Then shares are recomputed locally."""
    rows = [_row(1, specialty="Urologists"), _row(2, specialty="GP"), _row(3, specialty="Urologists")]
    assignments = [
        rta.RowTopicAssignment(row_id=1, scope_id="atc4:G04C2", brand="THRUPAS", topic_id="T1", topic_set_version="v1", prompt_version="row_topic_v1", batch_id="b1"),
        rta.RowTopicAssignment(row_id=2, scope_id="atc4:G04C2", brand="THRUPAS", topic_id="T2", topic_set_version="v1", prompt_version="row_topic_v1", batch_id="b1"),
        rta.RowTopicAssignment(row_id=3, scope_id="atc4:G04C2", brand="THRUPAS", topic_id="T1", topic_set_version="v1", prompt_version="row_topic_v1", batch_id="b1"),
    ]

    shares = rta.aggregate_topic_shares(rows, assignments, filters=rta.AssignmentFilters(specialties=("Urologists",)))

    assert [share.as_payload() for share in shares] == [
        {"topic_id": "T1", "label": "", "affected_row_count": 2, "share_pct": 100.0}
    ]


def test_runner_checkpoint_skips_completed_batches_and_plans_remaining_calls(tmp_path: Path) -> None:
    """Given a checkpoint, When batches are planned, Then completed batch ids are not re-called."""
    rows = [_row(i) for i in range(1, 6)]
    checkpoint = tmp_path / "checkpoint.jsonl"
    checkpoint.write_text(json.dumps({"batch_id": "atc4:G04C2:THRUPAS:row_topic_v1:000001", "status": "ok"}) + "\n", encoding="utf-8")

    plan = row_topic_runner.plan_batches(rows, batch_size=2, prompt_version="row_topic_v1", checkpoint_path=checkpoint)

    assert [batch.batch_id for batch in plan.pending_batches] == [
        "atc4:G04C2:THRUPAS:row_topic_v1:000002",
        "atc4:G04C2:THRUPAS:row_topic_v1:000003",
    ]
    assert plan.estimated_calls == 2


def test_runner_plans_batches_per_scope_brand_pair() -> None:
    """Given mixed scope-brand rows, When planned, Then batches never mix market-brand groups."""
    rows = [
        _row(3, brand="B"),
        _row(1, brand="A"),
        _row(2, brand="A"),
        rta.AssignmentInputRow(row_id=4, scope_id="atc4:OTHER", brand="A", keyword_text="message 4"),
    ]

    plan = row_topic_runner.plan_batches(
        rows,
        batch_size=10,
        prompt_version="row_topic_v1",
        checkpoint_path=Path("/tmp/nonexistent-row-topic-checkpoint.jsonl"),
    )

    assert plan.total_scope_brand_pairs == 3
    assert [batch.batch_id for batch in plan.pending_batches] == [
        "atc4:G04C2:A:row_topic_v1:000001",
        "atc4:G04C2:B:row_topic_v1:000001",
        "atc4:OTHER:A:row_topic_v1:000001",
    ]
    assert [[row.row_id for row in batch.rows] for batch in plan.pending_batches] == [[1, 2], [3], [4]]


def test_sql_contract_declares_idempotent_assignment_table_and_compatible_view() -> None:
    """Given row-topic SQL assets, When inspected, Then versioned idempotence and view contracts exist."""
    ddl = row_topic_sql.assignment_table_ddl("jw_brand_activity_stage")
    view = row_topic_sql.compatible_share_view_sql("jw_brand_activity_stage")

    assert "PRIMARY KEY (row_id, topic_id, topic_set_version)" in ddl
    assert "row_topic_assignment" in ddl
    assert "affected_row_count" in view
    assert "brand_total_rows" in view
    assert "ROUND(COUNT(DISTINCT a.row_id) * 100.0 / brand_total.brand_total_rows, 2)" in view
