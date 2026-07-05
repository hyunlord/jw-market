from __future__ import annotations

import json
from pathlib import Path
from types import TracebackType

import pytest

from pipeline.scripts.analysis.brand_activity.auto_topic import row_topic_assignment as rta
from pipeline.scripts.analysis.brand_activity.auto_topic import row_topic_db
from pipeline.scripts.analysis.brand_activity.auto_topic import row_topic_execute
from pipeline.scripts.analysis.brand_activity.auto_topic import row_topic_runner
from pipeline.scripts.analysis.brand_activity.auto_topic import row_topic_sql


def _row(row_id: int, brand: str = "THRUPAS", period: str = "2026-05", specialty: str = "Urologists") -> rta.AssignmentInputRow:
    return rta.AssignmentInputRow(
        row_id=row_id,
        scope_id="atc4:G04C2",
        brand=brand,
        keyword_text=f"message {row_id}",
        stage_row_sha256=f"hash-{row_id}",
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


def test_parse_assignment_response_treats_string_empty_list_as_none() -> None:
    """Given a model emits the explicit none sentinel as a string, Then no topic is invented."""
    parsed = rta.parse_assignment_response(
        '{"assignments":[{"row_id":1,"topics":["[]"]},{"row_id":2,"topics":["T1"]}]}',
        [_row(1), _row(2)],
        {"T1"},
        "v1",
        "b1",
    )

    assert [item.row_id for item in parsed] == [2]
    assert [item.topic_id for item in parsed] == ["T1"]


def test_parse_assignment_response_allow_missing_returns_assignments_and_missing_ids() -> None:
    """Given a model omits one row, When parsed for fallback, Then parsed rows are kept without guessing."""
    parsed = rta.parse_assignment_response_allow_missing(
        '{"assignments":[{"row_id":1,"topics":["T1"]},{"row_id":3,"topics":[]}]}',
        [_row(1), _row(2), _row(3)],
        {"T1"},
        "v1",
        "b1",
    )

    assert parsed.missing_row_ids == (2,)
    assert [item.row_id for item in parsed.assignments] == [1]
    assert [item.topic_id for item in parsed.assignments] == ["T1"]


class _FakeAssignmentClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.calls: list[list[dict[str, str]]] = []

    def classify(self, messages: list[dict[str, str]]) -> tuple[str, dict[str, int], int]:
        self.calls.append(messages)
        return self.responses.pop(0), {}, 1


def test_execute_falls_back_to_missing_rows_only() -> None:
    """Given a partial batch response, When one row is missing, Then only that row is re-asked."""
    rows = (_row(1), _row(2), _row(3))
    rubric = (rta.TopicRubric(topic_id="T1", label="axis", definition="axis"),)
    client = _FakeAssignmentClient(
        [
            '{"assignments":[{"row_id":1,"topics":["T1"]},{"row_id":2,"topics":[]}]}',
            '{"assignments":[{"row_id":3,"topics":["T1"]}]}',
        ]
    )

    parsed = row_topic_execute._classify_with_missing_fallback(  # noqa: SLF001 - regression covers resume-critical private helper.
        client,
        rubric,
        rows,
        "topic-set",
        "batch-1",
        max_calls=10,
        calls_used=0,
    )

    assert parsed["calls"] == 2
    assert parsed["fallback_calls"] == 1
    assert parsed["missing_row_ids"] == []
    assert [(item.row_id, item.topic_id) for item in parsed["assignments"]] == [(1, "T1"), (3, "T1")]
    assert "3\tTHRUPAS" in client.calls[1][1]["content"]
    assert "1\tTHRUPAS" not in client.calls[1][1]["content"]


def test_execute_records_unresolved_missing_after_small_fallback() -> None:
    """Given fallback still omits a row, When classified, Then the row id is recorded but not guessed."""
    rows = (_row(1), _row(2))
    rubric = (rta.TopicRubric(topic_id="T1", label="axis", definition="axis"),)
    client = _FakeAssignmentClient(
        [
            '{"assignments":[{"row_id":1,"topics":["T1"]}]}',
            '{"assignments":[]}',
            '{"assignments":[]}',
        ]
    )

    parsed = row_topic_execute._classify_with_missing_fallback(  # noqa: SLF001 - regression covers no-imputation fallback.
        client,
        rubric,
        rows,
        "topic-set",
        "batch-1",
        max_calls=10,
        calls_used=0,
    )

    assert parsed["calls"] == 3
    assert parsed["fallback_calls"] == 2
    assert parsed["missing_row_ids"] == [2]
    assert [(item.row_id, item.topic_id) for item in parsed["assignments"]] == [(1, "T1")]


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


def test_execute_rubric_combines_axis_with_only_that_brand_topics() -> None:
    """Given a stored scope payload, When rubrics are built, Then brand topics stay brand-local."""
    payload = {
        "axis": {"topics": [{"topic_id": "T1", "label": "axis", "definition": "common"}]},
        "brands": [
            {"brand": "A", "brand_specific_topics": [{"topic_id": "A:B1", "label": "A only", "definition": "A"}]},
            {"brand": "B", "brand_specific_topics": [{"topic_id": "B:B1", "label": "B only", "definition": "B"}]},
        ],
    }
    scope = row_topic_db.ScopeRubric(
        scope_id="atc4:G04C2",
        display_name="G04C2",
        atc4_values=("G04C2",),
        axis_topics=(row_topic_db.topic_rubric(payload["axis"]["topics"][0]),),
        brand_topics={
            "A": (row_topic_db.topic_rubric(payload["brands"][0]["brand_specific_topics"][0]),),
            "B": (row_topic_db.topic_rubric(payload["brands"][1]["brand_specific_topics"][0]),),
        },
    )

    rubrics = {
        (scope.scope_id, brand): (*scope.axis_topics, *brand_topics)
        for brand, brand_topics in scope.brand_topics.items()
    }

    assert [topic.topic_id for topic in rubrics[("atc4:G04C2", "A")]] == ["T1", "A:B1"]
    assert [topic.topic_id for topic in rubrics[("atc4:G04C2", "B")]] == ["T1", "B:B1"]


def test_execute_rubric_uses_axis_only_for_payload_external_brands() -> None:
    """Given a market row for a brand without brand-specific topics, Then only axis topics are offered."""
    scope = row_topic_db.ScopeRubric(
        scope_id="atc4:G04C2",
        display_name="G04C2",
        atc4_values=("G04C2",),
        axis_topics=(rta.TopicRubric(topic_id="T1", label="axis", definition="common"),),
        brand_topics={"THRUPAS": (rta.TopicRubric(topic_id="THRUPAS:B1", label="brand", definition="brand"),)},
    )
    rows = [_row(1, brand="THRUPAS"), _row(2, brand="OTHER")]
    rubrics = {}
    for row in rows:
        brand_topics = scope.brand_topics.get(row.brand, ())
        rubrics[(scope.scope_id, row.brand)] = (*scope.axis_topics, *brand_topics)

    assert [topic.topic_id for topic in rubrics[("atc4:G04C2", "THRUPAS")]] == ["T1", "THRUPAS:B1"]
    assert [topic.topic_id for topic in rubrics[("atc4:G04C2", "OTHER")]] == ["T1"]


def test_sql_contract_declares_idempotent_assignment_table_and_compatible_view() -> None:
    """Given row-topic SQL assets, When inspected, Then versioned idempotence and view contracts exist."""
    ddl = row_topic_sql.assignment_table_ddl("jw_brand_activity_stage")
    view = row_topic_sql.compatible_share_view_sql("jw_brand_activity_stage")

    assert "PRIMARY KEY (row_id, topic_id, topic_set_version)" in ddl
    assert "row_topic_assignment" in ddl
    assert "affected_row_count" in view
    assert "brand_total_rows" in view
    assert "mart_brand_activity_topics topic_scope" in view
    assert "JSON_CONTAINS(topic_scope.atc4_values, JSON_QUOTE(k.therapeutic_class), '$')" in view
    assert "ROUND(COUNT(DISTINCT a.row_id) * 100.0 / brand_total.brand_total_rows, 2)" in view


class _FakeCursor:
    def __init__(self, rows: list[dict[str, str | int]] | None = None, *, fail_on_status_insert: bool = False) -> None:
        self.rows = rows or []
        self.fail_on_status_insert = fail_on_status_insert
        self.statements: list[str] = []
        self.executemany_values: list[list[tuple[str | int, ...]]] = []

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, _exc_type: type[BaseException] | None, _exc: BaseException | None, _tb: TracebackType | None) -> None:
        return None

    def execute(self, sql: str, _params: tuple[str, ...] | None = None) -> int:
        self.statements.append(sql)
        return 1

    def executemany(self, sql: str, values: list[tuple[str | int, ...]]) -> int:
        self.statements.append(sql)
        if self.fail_on_status_insert and "row_topic_assignment_status" in sql:
            raise row_topic_db.AssignmentStatusError("status insert failed")
        self.executemany_values.append(values)
        return len(values)

    def fetchall(self) -> list[dict[str, str | int]]:
        return self.rows


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor) -> None:
        self.cursor_instance = cursor
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> _FakeCursor:
        return self.cursor_instance

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def test_db_pending_status_records_none_only_rows_as_classified() -> None:
    """Given a none-only row, When status rows are built, Then it is marked complete with zero assignments."""
    rows = (_row(1), _row(2))
    assignments = [
        rta.RowTopicAssignment(
            row_id=2,
            scope_id="atc4:G04C2",
            brand="THRUPAS",
            topic_id="T1",
            topic_set_version="topic-set",
            prompt_version="row_topic_v1",
            batch_id="batch-1",
        )
    ]

    statuses = row_topic_execute._status_rows_for_batch(  # noqa: SLF001 - regression covers DB pending contract.
        rows,
        assignments,
        missing_row_ids=[],
        topic_set_version="topic-set",
        batch_id="batch-1",
    )

    assert [(item.row_id, item.status, item.assignment_count) for item in statuses] == [
        (1, row_topic_db.STATUS_CLASSIFIED, 0),
        (2, row_topic_db.STATUS_CLASSIFIED, 1),
    ]


def test_db_pending_excludes_none_only_rows_and_reopens_changed_or_retry_unresolved_rows() -> None:
    """Given status rows, When pending is loaded, Then completed none rows stay skipped unless content changed."""
    rows = [_row(1), _row(2), _row(3)]
    cursor = _FakeCursor(
        [
            {"scope_id": "atc4:G04C2", "row_id": 1, "stage_row_sha256": "hash-1", "status": row_topic_db.STATUS_CLASSIFIED},
            {"scope_id": "atc4:G04C2", "row_id": 2, "stage_row_sha256": "hash-2", "status": row_topic_db.STATUS_UNRESOLVED_MISSING},
            {"scope_id": "atc4:G04C2", "row_id": 3, "stage_row_sha256": "old-hash", "status": row_topic_db.STATUS_CLASSIFIED},
        ]
    )
    connection = _FakeConnection(cursor)

    default_pending = row_topic_db.load_pending_rows(connection, schema="jw_brand_activity_stage", topic_set_version="topic-set", rows=rows)
    retry_pending = row_topic_db.load_pending_rows(
        connection,
        schema="jw_brand_activity_stage",
        topic_set_version="topic-set",
        rows=rows,
        retry_unresolved=True,
    )

    assert [row.row_id for row in default_pending] == [3]
    assert [row.row_id for row in retry_pending] == [2, 3]


def test_db_pending_transaction_rolls_back_assignments_when_status_insert_fails() -> None:
    """Given status persistence fails, When a DB-pending batch is stored, Then assignments roll back too."""
    cursor = _FakeCursor(fail_on_status_insert=True)
    connection = _FakeConnection(cursor)
    assignment = rta.RowTopicAssignment(
        row_id=1,
        scope_id="atc4:G04C2",
        brand="THRUPAS",
        topic_id="T1",
        topic_set_version="topic-set",
        prompt_version="row_topic_v1",
        batch_id="batch-1",
    )
    status = row_topic_db.RowTopicAssignmentStatus(
        topic_set_version="topic-set",
        scope_id="atc4:G04C2",
        row_id=1,
        stage_row_sha256="hash-1",
        prompt_version="row_topic_v1",
        batch_id="batch-1",
        status=row_topic_db.STATUS_CLASSIFIED,
        assignment_count=1,
    )

    with pytest.raises(row_topic_db.AssignmentStatusError):
        row_topic_db.insert_assignment_batch(
            connection,
            schema="jw_brand_activity_stage",
            assignments=[assignment],
            statuses=[status],
        )

    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_runner_can_plan_db_pending_rows_without_checkpoint_skip(tmp_path: Path) -> None:
    """Given DB pending rows, When planned, Then checkpoint files remain audit-only for db mode."""
    rows = [_row(i) for i in range(1, 4)]
    checkpoint = tmp_path / "checkpoint.jsonl"
    checkpoint.write_text(json.dumps({"batch_id": "atc4:G04C2:THRUPAS:row_topic_v1:000001", "status": "ok"}) + "\n", encoding="utf-8")

    plan = row_topic_runner.plan_batches(
        rows,
        batch_size=2,
        prompt_version="row_topic_v1",
        checkpoint_path=checkpoint,
        ignore_checkpoint=True,
    )

    assert [batch.batch_id for batch in plan.pending_batches] == [
        "atc4:G04C2:THRUPAS:row_topic_v1:000001",
        "atc4:G04C2:THRUPAS:row_topic_v1:000002",
    ]
