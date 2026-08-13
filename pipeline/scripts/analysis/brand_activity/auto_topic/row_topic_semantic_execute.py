from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import pymysql

from .row_topic_semantic_db import (
    insert_semantic_batch,
    load_existing_semantic_results,
    mark_semantic_batch_complete,
    record_semantic_batch_failure,
    start_semantic_batch,
)
from .row_topic_semantic_runner import (
    OccurrenceResult,
    SemanticBatch,
    reconcile_occurrence_results,
    select_semantic_work,
)


@dataclass(frozen=True, slots=True)
class SemanticBatchOutcome:
    batch_id: str
    status: str
    assignment_rows: int
    status_rows: int
    calls_used: int = 0
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class SemanticClassification:
    results: tuple[OccurrenceResult, ...]
    calls_used: int
    raw_responses: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FailedResponseRecord:
    run_id: str
    batch_id: str
    error_code: str
    responses: tuple[str, ...]
    recorded_at_utc_naive: str


def execute_semantic_batch(
    connection: pymysql.connections.Connection,
    *,
    schema: str,
    run_id: str,
    batch: SemanticBatch,
    topic_set_version: str,
    prompt_version: str,
    classified_at_utc_naive: str,
    classify: Callable[[SemanticBatch], SemanticClassification | tuple[OccurrenceResult, ...]],
    preserve_failed_response: Callable[[FailedResponseRecord], None] | None = None,
) -> SemanticBatchOutcome:
    """Classify and persist one batch, durably exposing partial failure."""
    should_execute = start_semantic_batch(
        connection,
        schema=schema,
        run_id=run_id,
        batch=batch,
        started_at_utc_naive=classified_at_utc_naive,
    )
    if not should_execute:
        return SemanticBatchOutcome(
            batch_id=batch.batch_id,
            status="complete",
            assignment_rows=0,
            status_rows=0,
            calls_used=0,
        )
    raw_responses: tuple[str, ...] = ()
    try:
        existing_results = load_existing_semantic_results(
            connection,
            schema=schema,
            batch=batch,
            topic_set_version=topic_set_version,
        )
        selection = select_semantic_work(
            batch,
            topic_set_version=topic_set_version,
            existing_results=existing_results,
        )
        classification_batch = selection.classification_batch
        if classification_batch is None:
            mark_semantic_batch_complete(
                connection,
                schema=schema,
                run_id=run_id,
                batch_id=batch.batch_id,
                calls_used=0,
                finished_at_utc_naive=classified_at_utc_naive,
            )
            return SemanticBatchOutcome(
                batch_id=batch.batch_id,
                status="complete",
                assignment_rows=0,
                status_rows=0,
                calls_used=0,
            )
        classified = classify(classification_batch)
        if isinstance(classified, SemanticClassification):
            results = classified.results
            calls_used = classified.calls_used
            raw_responses = classified.raw_responses
        else:
            results = classified
            calls_used = 1
        reconciled = reconcile_occurrence_results(
            classification_batch.occurrences,
            results,
            topic_set_version=topic_set_version,
            prompt_version=prompt_version,
            batch_id=batch.batch_id,
        )
        assignment_rows, status_rows = insert_semantic_batch(
            connection,
            schema=schema,
            assignments=reconciled.assignments,
            statuses=reconciled.statuses,
            assigned_at_utc_naive=classified_at_utc_naive,
        )
        mark_semantic_batch_complete(
            connection,
            schema=schema,
            run_id=run_id,
            batch_id=batch.batch_id,
            calls_used=calls_used,
            finished_at_utc_naive=classified_at_utc_naive,
        )
    except Exception as exc:
        failed_calls = int(getattr(exc, "calls_used", 0))
        error_code = type(exc).__name__
        error_message = str(exc)[:1000]
        exception_responses = getattr(exc, "raw_responses", ())
        if exception_responses and isinstance(exception_responses, tuple) and all(
            isinstance(item, str) for item in exception_responses
        ):
            raw_responses = exception_responses
        record_semantic_batch_failure(
            connection,
            schema=schema,
            run_id=run_id,
            batch_id=batch.batch_id,
            error_code=error_code,
            error_message=error_message,
            finished_at_utc_naive=classified_at_utc_naive,
            calls_used=failed_calls,
        )
        if preserve_failed_response is not None and raw_responses:
            try:
                preserve_failed_response(
                    FailedResponseRecord(
                        run_id=run_id,
                        batch_id=batch.batch_id,
                        error_code=error_code,
                        responses=raw_responses,
                        recorded_at_utc_naive=classified_at_utc_naive,
                    )
                )
            except OSError as evidence_error:
                raise ExceptionGroup(
                    "semantic batch failed and its response evidence could not be preserved",
                    (exc, evidence_error),
                ) from evidence_error
        raise
    return SemanticBatchOutcome(
        batch_id=batch.batch_id,
        status="complete",
        assignment_rows=assignment_rows,
        status_rows=status_rows,
        calls_used=calls_used,
    )
