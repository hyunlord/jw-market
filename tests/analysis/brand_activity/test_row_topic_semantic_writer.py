from __future__ import annotations

from dataclasses import replace
import json

import pytest

from pipeline.scripts.analysis.brand_activity.auto_topic import row_topic_semantic_db as semantic_db
from pipeline.scripts.analysis.brand_activity.auto_topic import row_topic_semantic_execute as semantic_execute
from pipeline.scripts.analysis.brand_activity.auto_topic import row_topic_semantic_runner as semantic_runner


GENERATION = "a" * 64


def _occurrence(row_id: int, semantic_key: str = "b" * 64) -> semantic_runner.SemanticOccurrence:
    return semantic_runner.SemanticOccurrence(
        stage_generation_id=GENERATION,
        stage_row_id=row_id,
        semantic_event_key_v1=semantic_key,
        scope_id="atc4:C10AA",
        brand="LIVALOZET",
    )


def _result(row_id: int, topics: tuple[str, ...]) -> semantic_runner.OccurrenceResult:
    return semantic_runner.OccurrenceResult(stage_row_id=row_id, topic_ids=topics)


def test_red_duplicate_occurrences_require_bridge_fanout() -> None:
    occurrences = (_occurrence(11), _occurrence(12))

    reconciled = semantic_runner.reconcile_occurrence_results(
        occurrences,
        (_result(11, ("T1",)), _result(12, ("T1",))),
        topic_set_version="topics-v1",
        prompt_version="prompt-v1",
        batch_id="batch-1",
    )

    assert len(reconciled.assignments) == 1
    assert len(reconciled.statuses) == 1
    assert reconciled.covered_occurrence_count == 2
    assert reconciled.covered_stage_row_ids == (11, 12)


def test_conflicting_duplicate_occurrences_fail_closed() -> None:
    with pytest.raises(semantic_runner.SemanticOccurrenceConflict, match="SEMANTIC_OCCURRENCE_CONFLICT"):
        semantic_runner.reconcile_occurrence_results(
            (_occurrence(11), _occurrence(12)),
            (_result(11, ("T1",)), _result(12, ("T2",))),
            topic_set_version="topics-v1",
            prompt_version="prompt-v1",
            batch_id="batch-1",
        )


def test_batch_id_is_deterministic_and_occurrence_sensitive() -> None:
    first = semantic_runner.build_semantic_batches(
        (_occurrence(11), _occurrence(12)),
        topic_set_version="topics-v1",
        prompt_version="prompt-v1",
        wave_no=1,
        batch_size=150,
    )
    repeated = semantic_runner.build_semantic_batches(
        (_occurrence(11), _occurrence(12)),
        topic_set_version="topics-v1",
        prompt_version="prompt-v1",
        wave_no=1,
        batch_size=150,
    )
    changed = semantic_runner.build_semantic_batches(
        (_occurrence(11), _occurrence(13)),
        topic_set_version="topics-v1",
        prompt_version="prompt-v1",
        wave_no=1,
        batch_size=150,
    )

    assert first == repeated
    assert first[0].batch_id != changed[0].batch_id
    assert first[0].occurrence_sha256 != changed[0].occurrence_sha256


class _Cursor:
    def __init__(self, rowcounts: list[int], rows: list[dict[str, object] | None] | None = None) -> None:
        self._rowcounts = rowcounts
        self._rows = rows or []
        self.rowcount = 0
        self.executed: list[tuple[str, tuple[object, ...]]] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[object, ...]) -> int:
        self.executed.append((sql, params))
        self.rowcount = self._rowcounts.pop(0)
        return self.rowcount

    def fetchone(self) -> dict[str, object] | None:
        return self._rows.pop(0) if self._rows else None


class _Connection:
    def __init__(
        self,
        rowcounts: list[int],
        rows: list[dict[str, object] | None] | None = None,
    ) -> None:
        self.cursor_value = _Cursor(rowcounts, rows)
        self.commits = 0
        self.rollbacks = 0
        self.begins = 0

    def cursor(self) -> _Cursor:
        return self.cursor_value

    def begin(self) -> None:
        self.begins += 1

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def test_cas_generation_mismatch_is_a_typed_failure() -> None:
    connection = _Connection([0])

    with pytest.raises(semantic_db.ReleaseCasConflict, match="affected_rows=0"):
        semantic_db.cas_active_release(
            connection,
            schema="jw_brand_activity_stage",
            pointer_name="brand_activity_keyword",
            expected_generation=7,
            expected_active_release_id=None,
            new_release_id="release-2",
            actor="test",
            now_utc_naive="2026-08-12 04:00:00.000000",
        )

    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_cas_match_changes_exactly_one_row() -> None:
    connection = _Connection([1])

    affected = semantic_db.cas_active_release(
        connection,
        schema="jw_brand_activity_stage",
        pointer_name="brand_activity_keyword",
        expected_generation=7,
        expected_active_release_id="release-1",
        new_release_id="release-2",
        actor="test",
        now_utc_naive="2026-08-12 04:00:00.000000",
    )

    assert affected == 1
    assert connection.commits == 1
    assert connection.rollbacks == 0
    sql, params = connection.cursor_value.executed[0]
    assert "generation=%s" in sql
    assert "active_release_id <=> %s" in sql
    assert params[-2:] == (7, "release-1")


def test_concurrent_cas_allows_only_one_winner() -> None:
    winner = _Connection([1])
    loser = _Connection([0])
    kwargs = {
        "schema": "jw_brand_activity_stage",
        "pointer_name": "brand_activity_keyword",
        "expected_generation": 0,
        "expected_active_release_id": None,
        "new_release_id": "release-1",
        "actor": "test",
        "now_utc_naive": "2026-08-12 04:00:00.000000",
    }

    assert semantic_db.cas_active_release(winner, **kwargs) == 1
    with pytest.raises(semantic_db.ReleaseCasConflict):
        semantic_db.cas_active_release(loser, **kwargs)

    assert (winner.commits, loser.commits, loser.rollbacks) == (1, 0, 1)


def test_batch_start_is_idempotent_only_for_same_running_input() -> None:
    batch = semantic_runner.build_semantic_batches(
        (_occurrence(11), _occurrence(12)),
        topic_set_version="topics-v1",
        prompt_version="prompt-v1",
        wave_no=1,
        batch_size=150,
    )[0]
    connection = _Connection(
        [0],
        [
            {
                "wave_no": 1,
                "scope_id": batch.scope_id,
                "brand": batch.brand,
                "occurrence_count": 2,
                "semantic_key_count": 1,
                "occurrence_sha256": batch.occurrence_sha256,
                "status": "running",
            }
        ],
    )

    should_execute = semantic_db.start_semantic_batch(
        connection,  # type: ignore[arg-type]
        schema="jw_brand_activity_stage",
        run_id="run-1",
        batch=batch,
        started_at_utc_naive="2026-08-12 04:00:00.000000",
    )

    assert should_execute is True
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert len(connection.cursor_value.executed) == 1


def test_run_completion_preserves_partial_failure() -> None:
    connection = _Connection([1])

    semantic_db.finish_semantic_run(
        connection,  # type: ignore[arg-type]
        schema="jw_brand_activity_stage",
        run_id="run-1",
        calls_used=8,
        failed_batches=2,
        finished_at_utc_naive="2026-08-12 04:00:00.000000",
    )

    sql, params = connection.cursor_value.executed[0]
    assert "status=%s" in sql
    assert params[0] == "partial_failed"
    assert params[2] == 2


def test_completed_batch_rerun_is_a_noop() -> None:
    batch = semantic_runner.build_semantic_batches(
        (_occurrence(11),),
        topic_set_version="topics-v1",
        prompt_version="prompt-v1",
        wave_no=1,
        batch_size=150,
    )[0]
    connection = _Connection(
        [0],
        [
            {
                "wave_no": 1,
                "scope_id": batch.scope_id,
                "brand": batch.brand,
                "occurrence_count": 1,
                "semantic_key_count": 1,
                "occurrence_sha256": batch.occurrence_sha256,
                "status": "complete",
            }
        ],
    )

    should_execute = semantic_db.start_semantic_batch(
        connection,  # type: ignore[arg-type]
        schema="jw_brand_activity_stage",
        run_id="run-1",
        batch=batch,
        started_at_utc_naive="2026-08-12 04:00:00.000000",
    )

    assert should_execute is False
    assert connection.commits == 1
    assert len(connection.cursor_value.executed) == 1


@pytest.mark.parametrize(
    ("operation", "expected_status"),
    [
        (semantic_db.mark_semantic_batch_complete, "complete"),
        (semantic_db.record_semantic_batch_failure, "failed"),
    ],
)
def test_batch_terminal_updates_require_exactly_one_running_row(
    operation: object,
    expected_status: str,
) -> None:
    success = _Connection([1])
    common = {
        "schema": "jw_brand_activity_stage",
        "run_id": "run-1",
        "batch_id": "batch-1",
        "finished_at_utc_naive": "2026-08-12 04:00:00.000000",
    }
    if expected_status == "complete":
        semantic_db.mark_semantic_batch_complete(success, calls_used=1, **common)  # type: ignore[arg-type]
    else:
        semantic_db.record_semantic_batch_failure(
            success, error_code="RuntimeError", error_message="failed", **common  # type: ignore[arg-type]
        )
    assert success.commits == 1
    assert expected_status in success.cursor_value.executed[0][0]

    stale = _Connection([0])
    with pytest.raises(semantic_db.ImmutableResultConflict, match="affected_rows=0"):
        if expected_status == "complete":
            semantic_db.mark_semantic_batch_complete(stale, calls_used=1, **common)  # type: ignore[arg-type]
        else:
            semantic_db.record_semantic_batch_failure(
                stale, error_code="RuntimeError", error_message="failed", **common  # type: ignore[arg-type]
            )
    assert stale.rollbacks == 1


def test_immutable_result_conflict_is_not_overwritten() -> None:
    assignment = semantic_runner.reconcile_occurrence_results(
        (_occurrence(11),),
        (_result(11, ("T1",)),),
        topic_set_version="topics-v1",
        prompt_version="prompt-v1",
        batch_id="batch-1",
    ).assignments[0]

    semantic_db.assert_immutable_assignment_compatible(
        assignment,
        replace(assignment, prompt_version="prompt-v2", batch_id="batch-2"),
    )
    with pytest.raises(semantic_db.ImmutableResultConflict, match="IMMUTABLE_RESULT_CONFLICT"):
        semantic_db.assert_immutable_assignment_compatible(
            assignment,
            replace(assignment, topic_id="T2"),
        )


def test_exact_topic_set_is_the_immutable_result_contract() -> None:
    status = semantic_runner.reconcile_occurrence_results(
        (_occurrence(11),),
        (_result(11, ("T2", "T3", "T5")),),
        topic_set_version="topics-v1",
        prompt_version="prompt-v1",
        batch_id="batch-1",
    ).statuses[0]

    semantic_db.assert_exact_semantic_result_compatible(
        expected_topic_ids=("T2", "T3", "T5"),
        expected_status=status,
        existing_topic_ids=("T5", "T3", "T2"),
        existing_status=replace(status, batch_id="batch-2", prompt_version="prompt-v2"),
    )
    with pytest.raises(semantic_db.ImmutableResultConflict):
        semantic_db.assert_exact_semantic_result_compatible(
            expected_topic_ids=("T1", "T2", "T3", "T5"),
            expected_status=replace(status, assignment_count=4),
            existing_topic_ids=("T2", "T3", "T5"),
            existing_status=status,
        )


def test_assignment_count_drift_is_integrity_error_not_content_conflict() -> None:
    status = semantic_runner.reconcile_occurrence_results(
        (_occurrence(11),),
        (_result(11, ("T2", "T3", "T5")),),
        topic_set_version="topics-v1",
        prompt_version="prompt-v1",
        batch_id="batch-1",
    ).statuses[0]

    with pytest.raises(semantic_db.SemanticResultIntegrityError, match="assignment_count"):
        semantic_db.assert_exact_semantic_result_compatible(
            expected_topic_ids=("T2", "T3", "T5"),
            expected_status=replace(status, assignment_count=4),
            existing_topic_ids=("T2", "T3", "T5"),
            existing_status=status,
        )


def test_status_exact_rerun_is_noop_and_new_generation_only_refreshes_audit() -> None:
    status = semantic_runner.reconcile_occurrence_results(
        (_occurrence(11),),
        (_result(11, ("T1",)),),
        topic_set_version="topics-v1",
        prompt_version="prompt-v1",
        batch_id="batch-1",
    ).statuses[0]

    assert semantic_db.assert_immutable_status_compatible(status, status) is False
    assert semantic_db.assert_immutable_status_compatible(
        replace(status, prompt_version="prompt-v2", batch_id="batch-2"),
        status,
    ) is False
    assert semantic_db.assert_immutable_status_compatible(
        replace(status, classified_stage_generation_id="c" * 64, batch_id="batch-2"),
        status,
    ) is True
    with pytest.raises(semantic_db.ImmutableResultConflict):
        semantic_db.assert_immutable_status_compatible(replace(status, status="failed"), status)


def test_batch_write_rolls_back_assignments_when_status_insert_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TransactionConnection:
        def __init__(self) -> None:
            self.in_transaction = False
            self.pending_assignments = 0
            self.persisted_assignments = 0

        def begin(self) -> None:
            self.in_transaction = True

        def commit(self) -> None:
            self.persisted_assignments += self.pending_assignments
            self.pending_assignments = 0
            self.in_transaction = False

        def rollback(self) -> None:
            self.pending_assignments = 0
            self.in_transaction = False

    connection = TransactionConnection()

    def insert_assignments(*_args: object, **_kwargs: object) -> int:
        if connection.in_transaction:
            connection.pending_assignments += 1
        else:
            connection.persisted_assignments += 1
        return 1

    monkeypatch.setattr(semantic_db, "_insert_assignments", insert_assignments)
    monkeypatch.setattr(
        semantic_db,
        "_insert_statuses",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("forced status failure")),
    )

    with pytest.raises(RuntimeError, match="forced status failure"):
        semantic_db.insert_semantic_batch(
            connection,  # type: ignore[arg-type]
            schema="jw_brand_activity_stage",
            assignments=(),
            statuses=(),
            assigned_at_utc_naive="2026-08-12 04:00:00.000000",
        )

    assert connection.persisted_assignments == 0


def test_partial_failure_is_recorded_and_not_returned_as_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    batch = semantic_runner.build_semantic_batches(
        (_occurrence(11), _occurrence(12)),
        topic_set_version="topics-v1",
        prompt_version="prompt-v1",
        wave_no=1,
        batch_size=150,
    )[0]
    events: list[str] = []

    monkeypatch.setattr(
        semantic_execute,
        "start_semantic_batch",
        lambda *_args, **_kwargs: events.append("start") or True,
    )
    monkeypatch.setattr(
        semantic_execute,
        "load_existing_semantic_results",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(
        semantic_execute,
        "insert_semantic_batch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("db write failed")),
    )
    monkeypatch.setattr(
        semantic_execute,
        "record_semantic_batch_failure",
        lambda *_args, **_kwargs: events.append("failed"),
    )
    with pytest.raises(RuntimeError, match="db write failed"):
        semantic_execute.execute_semantic_batch(
            object(),  # type: ignore[arg-type]
            schema="jw_brand_activity_stage",
            run_id="run-1",
            batch=batch,
            topic_set_version="topics-v1",
            prompt_version="prompt-v1",
            classified_at_utc_naive="2026-08-12 04:00:00.000000",
            classify=lambda selected_batch: tuple(
                _result(item.stage_row_id, ("T1",)) for item in selected_batch.occurrences
            ),
        )

    assert events == ["start", "failed"]


def test_db_failure_preserves_the_classified_raw_response(monkeypatch: pytest.MonkeyPatch) -> None:
    batch = semantic_runner.build_semantic_batches(
        (_occurrence(11),),
        topic_set_version="topics-v1",
        prompt_version="prompt-v1",
        wave_no=1,
        batch_size=150,
    )[0]
    preserved: list[semantic_execute.FailedResponseRecord] = []
    monkeypatch.setattr(semantic_execute, "start_semantic_batch", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        semantic_execute,
        "load_existing_semantic_results",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(
        semantic_execute,
        "insert_semantic_batch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("db write failed")),
    )
    monkeypatch.setattr(semantic_execute, "record_semantic_batch_failure", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="db write failed"):
        semantic_execute.execute_semantic_batch(
            object(),  # type: ignore[arg-type]
            schema="jw_brand_activity_stage",
            run_id="run-1",
            batch=batch,
            topic_set_version="topics-v1",
            prompt_version="prompt-v1",
            classified_at_utc_naive="2026-08-12 04:00:00.000000",
            classify=lambda _batch: semantic_execute.SemanticClassification(
                results=(_result(11, ("T1",)),),
                calls_used=1,
                raw_responses=('{"topic_id":"T1"}',),
            ),
            preserve_failed_response=preserved.append,
        )

    assert len(preserved) == 1
    assert preserved[0].responses == ('{"topic_id":"T1"}',)
    assert preserved[0].error_code == "RuntimeError"


def test_failed_response_write_failure_does_not_block_batch_failure_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch = semantic_runner.build_semantic_batches(
        (_occurrence(11),),
        topic_set_version="topics-v1",
        prompt_version="prompt-v1",
        wave_no=1,
        batch_size=150,
    )[0]
    events: list[str] = []

    class ParseFailure(RuntimeError):
        calls_used = 1
        raw_responses = ('{"row_id":40351}',)

    monkeypatch.setattr(
        semantic_execute,
        "start_semantic_batch",
        lambda *_args, **_kwargs: events.append("start") or True,
    )
    monkeypatch.setattr(
        semantic_execute,
        "load_existing_semantic_results",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(
        semantic_execute,
        "record_semantic_batch_failure",
        lambda *_args, **_kwargs: events.append("failed"),
    )

    def fail_to_preserve(_record: semantic_execute.FailedResponseRecord) -> None:
        events.append("preserve")
        raise PermissionError("failed-response path is not writable")

    with pytest.raises(ExceptionGroup) as caught:
        semantic_execute.execute_semantic_batch(
            object(),  # type: ignore[arg-type]
            schema="jw_brand_activity_stage",
            run_id="run-1",
            batch=batch,
            topic_set_version="topics-v1",
            prompt_version="prompt-v1",
            classified_at_utc_naive="2026-08-12 04:00:00.000000",
            classify=lambda _batch: (_ for _ in ()).throw(ParseFailure("unexpected row_id: 40351")),
            preserve_failed_response=fail_to_preserve,
        )

    assert events == ["start", "failed", "preserve"]
    assert [type(error) for error in caught.value.exceptions] == [ParseFailure, PermissionError]
    assert "unexpected row_id: 40351" in str(caught.value.exceptions[0])
    assert "failed-response path is not writable" in str(caught.value.exceptions[1])


def test_success_closes_started_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    batch = semantic_runner.build_semantic_batches(
        (_occurrence(11),),
        topic_set_version="topics-v1",
        prompt_version="prompt-v1",
        wave_no=1,
        batch_size=150,
    )[0]
    events: list[str] = []
    monkeypatch.setattr(
        semantic_execute,
        "start_semantic_batch",
        lambda *_args, **_kwargs: events.append("start") or True,
    )
    monkeypatch.setattr(
        semantic_execute,
        "load_existing_semantic_results",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(semantic_execute, "insert_semantic_batch", lambda *_args, **_kwargs: (1, 1))
    monkeypatch.setattr(
        semantic_execute,
        "mark_semantic_batch_complete",
        lambda *_args, **_kwargs: events.append("complete"),
    )

    outcome = semantic_execute.execute_semantic_batch(
        object(),  # type: ignore[arg-type]
        schema="jw_brand_activity_stage",
        run_id="run-1",
        batch=batch,
        topic_set_version="topics-v1",
        prompt_version="prompt-v1",
        classified_at_utc_naive="2026-08-12 04:00:00.000000",
        classify=lambda _batch: (_result(11, ("T1",)),),
    )

    assert outcome.status == "complete"
    assert events == ["start", "complete"]


def test_lenient_row_id_counts_are_retained_on_complete_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch = semantic_runner.build_semantic_batches(
        (_occurrence(11), _occurrence(12, "c" * 64)),
        topic_set_version="topics-v1",
        prompt_version="prompt-v1",
        wave_no=1,
        batch_size=150,
    )[0]
    completed: dict[str, object] = {}
    monkeypatch.setattr(semantic_execute, "start_semantic_batch", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        semantic_execute,
        "load_existing_semantic_results",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(semantic_execute, "insert_semantic_batch", lambda *_args, **_kwargs: (1, 2))
    monkeypatch.setattr(
        semantic_execute,
        "mark_semantic_batch_complete",
        lambda *_args, **kwargs: completed.update(kwargs),
    )

    outcome = semantic_execute.execute_semantic_batch(
        object(),  # type: ignore[arg-type]
        schema="jw_brand_activity_stage",
        run_id="run-1",
        batch=batch,
        topic_set_version="topics-v1",
        prompt_version="prompt-v1",
        classified_at_utc_naive="2026-08-13 10:00:00.000000",
        classify=lambda _batch: semantic_execute.SemanticClassification(
            results=(_result(11, ("T1",)), _result(12, ())),
            calls_used=1,
            dropped_unexpected_row_ids=(40351,),
            dropped_missing_row_ids=(12,),
        ),
    )

    diagnostic = json.loads(str(completed["diagnostic_message"]))
    assert completed["diagnostic_code"] == "LENIENT_ROW_ID_DROP"
    assert diagnostic == {
        "missing_count": 1,
        "missing_row_ids": [12],
        "unexpected_count": 1,
        "unexpected_row_ids": [40351],
    }
    assert outcome.dropped_unexpected_count == 1
    assert outcome.dropped_missing_count == 1


def test_existing_semantic_identity_skips_llm_and_fans_out_through_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch = semantic_runner.build_semantic_batches(
        (_occurrence(11), _occurrence(12)),
        topic_set_version="topics-v1",
        prompt_version="prompt-v1",
        wave_no=1,
        batch_size=150,
    )[0]
    existing = semantic_runner.CanonicalSemanticResult(
        semantic_event_key_v1=batch.occurrences[0].semantic_event_key_v1,
        scope_id=batch.scope_id,
        topic_set_version="topics-v1",
        topic_ids=("T2", "T3", "T5"),
    )
    events: list[str] = []
    monkeypatch.setattr(semantic_execute, "start_semantic_batch", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        semantic_execute,
        "load_existing_semantic_results",
        lambda *_args, **_kwargs: (existing,),
    )
    monkeypatch.setattr(
        semantic_execute,
        "mark_semantic_batch_complete",
        lambda *_args, **_kwargs: events.append("complete"),
    )

    outcome = semantic_execute.execute_semantic_batch(
        object(),  # type: ignore[arg-type]
        schema="jw_brand_activity_stage",
        run_id="run-1",
        batch=batch,
        topic_set_version="topics-v1",
        prompt_version="prompt-v1",
        classified_at_utc_naive="2026-08-12 04:00:00.000000",
        classify=lambda _batch: pytest.fail("existing semantic identity must not call the LLM"),
    )

    assert outcome.calls_used == 0
    assert outcome.assignment_rows == 0
    assert outcome.status_rows == 0
    assert events == ["complete"]
