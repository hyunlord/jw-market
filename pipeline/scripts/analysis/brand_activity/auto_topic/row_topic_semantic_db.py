from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

import pymysql

from .row_topic_semantic_runner import (
    CanonicalSemanticResult,
    SemanticAssignment,
    SemanticBatch,
    SemanticOccurrence,
    SemanticStatus,
)
from .topic_store import validated_stage_schema


ASSIGNMENT_TABLE = "row_topic_assignment_semantic_v1"
STATUS_TABLE = "row_topic_assignment_status_semantic_v1"
RUN_TABLE = "row_topic_assignment_run_semantic_v1"
BATCH_TABLE = "row_topic_assignment_batch_semantic_v1"
ACTIVE_RELEASE_TABLE = "row_topic_taxonomy_active_release_v1"


class ImmutableResultConflict(RuntimeError):
    """Raised when an insert-only semantic result would be overwritten."""


class SemanticResultIntegrityError(RuntimeError):
    """Raised when derived semantic metadata disagrees with canonical content."""


class ReleaseCasConflict(RuntimeError):
    """Raised when the active release generation changed concurrently."""


class ConnectionLike(Protocol):
    def cursor(self) -> object: ...
    def begin(self) -> None: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...


def load_bridge_occurrences(
    connection: pymysql.connections.Connection,
    *,
    schema: str,
    stage_generation_id: str,
    topic_set_version: str,
    scope_ids: Sequence[str],
) -> tuple[SemanticOccurrence, ...]:
    """Read the complete requested generation in stable scope/brand/row order."""
    if not scope_ids:
        return ()
    safe_schema = validated_stage_schema(schema)
    placeholders = ",".join("%s" for _ in scope_ids)
    sql = f"""
        SELECT b.stage_generation_id, b.stage_row_id, b.semantic_event_key_v1,
               t.scope_id, k.product_name AS brand
        FROM `{safe_schema}`.`row_topic_stage_occurrence_v1` b
        JOIN `{safe_schema}`.`km_keyword_event_stage` k ON k.id=b.stage_row_id
        JOIN `{safe_schema}`.`mart_brand_activity_topics` t
          ON JSON_CONTAINS(t.atc4_values, JSON_QUOTE(k.therapeutic_class), '$')
        WHERE b.stage_generation_id=%s
          AND t.run_id=%s
          AND t.scope_id IN ({placeholders})
        ORDER BY t.scope_id, k.product_name, b.stage_row_id
    """
    with connection.cursor() as cursor:
        cursor.execute(sql, (stage_generation_id, topic_set_version, *scope_ids))
        rows = cursor.fetchall()
    return tuple(
        SemanticOccurrence(
            stage_generation_id=str(row["stage_generation_id"]),
            stage_row_id=int(row["stage_row_id"]),
            semantic_event_key_v1=str(row["semantic_event_key_v1"]),
            scope_id=str(row["scope_id"]),
            brand=str(row["brand"]),
        )
        for row in rows
    )


def assert_immutable_assignment_compatible(
    expected: SemanticAssignment,
    existing: SemanticAssignment,
) -> None:
    """Accept provenance drift while rejecting immutable topic-content drift."""
    expected_content = (
        expected.semantic_event_key_v1,
        expected.scope_id,
        expected.topic_id,
        expected.topic_set_version,
    )
    existing_content = (
        existing.semantic_event_key_v1,
        existing.scope_id,
        existing.topic_id,
        existing.topic_set_version,
    )
    if expected_content != existing_content:
        raise ImmutableResultConflict(
            "IMMUTABLE_RESULT_CONFLICT: existing semantic assignment differs from requested result"
        )


def assert_immutable_status_compatible(expected: SemanticStatus, existing: SemanticStatus) -> bool:
    """Return whether an equal result is from a newer bridge generation."""
    comparable_expected = (
        expected.semantic_event_key_v1,
        expected.scope_id,
        expected.topic_set_version,
        expected.status,
    )
    comparable_existing = (
        existing.semantic_event_key_v1,
        existing.scope_id,
        existing.topic_set_version,
        existing.status,
    )
    if comparable_expected != comparable_existing:
        raise ImmutableResultConflict(
            "IMMUTABLE_RESULT_CONFLICT: existing semantic status differs from requested result"
        )
    if expected.assignment_count != existing.assignment_count:
        raise SemanticResultIntegrityError(
            "SEMANTIC_RESULT_INTEGRITY_ERROR: assignment_count differs between equal semantic results"
        )
    if expected.classified_stage_generation_id == existing.classified_stage_generation_id:
        return False
    return True


def assert_exact_semantic_result_compatible(
    *,
    expected_topic_ids: Sequence[str],
    expected_status: SemanticStatus,
    existing_topic_ids: Sequence[str],
    existing_status: SemanticStatus,
) -> None:
    """Compare immutable identity and the complete canonical topic set."""
    expected_topics = tuple(sorted(set(expected_topic_ids)))
    existing_topics = tuple(sorted(set(existing_topic_ids)))
    expected_identity = (
        expected_status.semantic_event_key_v1,
        expected_status.scope_id,
        expected_status.topic_set_version,
    )
    existing_identity = (
        existing_status.semantic_event_key_v1,
        existing_status.scope_id,
        existing_status.topic_set_version,
    )
    if expected_identity != existing_identity or expected_topics != existing_topics:
        raise ImmutableResultConflict(
            "IMMUTABLE_RESULT_CONFLICT: existing canonical topic set differs from requested result"
        )
    expected_derived_status = "classified" if expected_topics else "unresolved_missing"
    existing_derived_status = "classified" if existing_topics else "unresolved_missing"
    if (
        expected_status.status != expected_derived_status
        or existing_status.status != existing_derived_status
    ):
        raise SemanticResultIntegrityError(
            "SEMANTIC_RESULT_INTEGRITY_ERROR: status differs from canonical topic set"
        )
    if (
        expected_status.assignment_count != len(expected_topics)
        or existing_status.assignment_count != len(existing_topics)
    ):
        raise SemanticResultIntegrityError(
            "SEMANTIC_RESULT_INTEGRITY_ERROR: assignment_count differs from canonical topic set"
        )


def load_existing_semantic_results(
    connection: pymysql.connections.Connection,
    *,
    schema: str,
    batch: SemanticBatch,
    topic_set_version: str,
) -> tuple[CanonicalSemanticResult, ...]:
    """Load complete stored results for identities present in one batch."""
    semantic_keys = tuple(sorted({item.semantic_event_key_v1 for item in batch.occurrences}))
    if not semantic_keys:
        return ()
    safe_schema = validated_stage_schema(schema)
    placeholders = ",".join("%s" for _ in semantic_keys)
    sql = f"""
        SELECT s.semantic_event_key_v1, s.scope_id, s.topic_set_version,
               s.status, s.assignment_count, a.topic_id
        FROM `{safe_schema}`.`{STATUS_TABLE}` s
        LEFT JOIN `{safe_schema}`.`{ASSIGNMENT_TABLE}` a
          ON a.semantic_event_key_v1=s.semantic_event_key_v1
         AND a.scope_id=s.scope_id
         AND a.topic_set_version=s.topic_set_version
        WHERE s.scope_id=%s AND s.topic_set_version=%s
          AND s.semantic_event_key_v1 IN ({placeholders})
        ORDER BY s.semantic_event_key_v1, a.topic_id
    """
    with connection.cursor() as cursor:
        cursor.execute(sql, (batch.scope_id, topic_set_version, *semantic_keys))
        rows = cursor.fetchall()

    status_by_identity: dict[tuple[str, str, str], tuple[str, int]] = {}
    topics_by_identity: dict[tuple[str, str, str], list[str]] = {}
    for row in rows:
        identity = (
            str(row["semantic_event_key_v1"]),
            str(row["scope_id"]),
            str(row["topic_set_version"]),
        )
        status_by_identity.setdefault(
            identity,
            (str(row["status"]), int(row["assignment_count"])),
        )
        topics = topics_by_identity.setdefault(identity, [])
        topic_id = row["topic_id"]
        if topic_id is not None:
            topics.append(str(topic_id))

    results: list[CanonicalSemanticResult] = []
    for identity in sorted(status_by_identity):
        status, assignment_count = status_by_identity[identity]
        topics = tuple(topics_by_identity[identity])
        derived_status = "classified" if topics else "unresolved_missing"
        if status != derived_status or assignment_count != len(topics):
            raise SemanticResultIntegrityError(
                "SEMANTIC_RESULT_INTEGRITY_ERROR: stored status/count differs from canonical topic set"
            )
        results.append(
            CanonicalSemanticResult(
                semantic_event_key_v1=identity[0],
                scope_id=identity[1],
                topic_set_version=identity[2],
                topic_ids=topics,
            )
        )
    return tuple(results)


def insert_semantic_batch(
    connection: pymysql.connections.Connection,
    *,
    schema: str,
    assignments: Sequence[SemanticAssignment],
    statuses: Sequence[SemanticStatus],
    assigned_at_utc_naive: str,
) -> tuple[int, int]:
    """Insert one semantic batch atomically after exact immutable conflict checks."""
    safe_schema = validated_stage_schema(schema)
    try:
        connection.begin()
        assignment_rows = _insert_assignments(
            connection,
            schema=safe_schema,
            assignments=assignments,
            assigned_at_utc_naive=assigned_at_utc_naive,
        )
        status_rows = _insert_statuses(
            connection,
            schema=safe_schema,
            statuses=statuses,
            classified_at_utc_naive=assigned_at_utc_naive,
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return assignment_rows, status_rows


def start_semantic_run(
    connection: pymysql.connections.Connection,
    *,
    schema: str,
    run_id: str,
    release_id: str,
    stage_generation_id: str,
    prompt_version: str,
    execution_mode: str,
    planned_occurrences: int,
    planned_calls: int,
    started_at_utc_naive: str,
    created_by: str,
) -> bool:
    """Create one run ledger row, accepting only an exact immutable rerun."""
    safe_schema = validated_stage_schema(schema)
    expected = (
        release_id,
        stage_generation_id,
        prompt_version,
        execution_mode,
        planned_occurrences,
        planned_calls,
        created_by,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                    SELECT release_id, stage_generation_id, prompt_version, execution_mode,
                           planned_occurrences, planned_calls, created_by, status
                    FROM `{safe_schema}`.`{RUN_TABLE}`
                    WHERE run_id=%s
                """,
                (run_id,),
            )
            row = cursor.fetchone()
            if row is not None:
                existing = (
                    str(row["release_id"]),
                    str(row["stage_generation_id"]),
                    str(row["prompt_version"]),
                    str(row["execution_mode"]),
                    int(row["planned_occurrences"]),
                    int(row["planned_calls"]),
                    str(row["created_by"]),
                )
                if existing != expected:
                    raise ImmutableResultConflict(
                        "IMMUTABLE_RESULT_CONFLICT: run identity has different input"
                    )
                status = str(row["status"])
                if status == "complete":
                    connection.commit()
                    return False
                if status == "partial_failed":
                    cursor.execute(
                        f"""
                            UPDATE `{safe_schema}`.`{RUN_TABLE}`
                            SET status='running', finished_at=NULL
                            WHERE run_id=%s AND status='partial_failed'
                        """,
                        (run_id,),
                    )
                    if int(cursor.rowcount) != 1:
                        raise ImmutableResultConflict(
                            "IMMUTABLE_RESULT_CONFLICT: partial run restart CAS failed"
                        )
                elif status != "running":
                    raise ImmutableResultConflict(
                        f"IMMUTABLE_RESULT_CONFLICT: unsupported run status {status!r}"
                    )
                connection.commit()
                return True
            cursor.execute(
                f"""
                    INSERT INTO `{safe_schema}`.`{RUN_TABLE}`
                      (run_id, release_id, stage_generation_id, prompt_version,
                       execution_mode, status, planned_occurrences, planned_calls,
                       started_at, created_by)
                    VALUES (%s,%s,%s,%s,%s,'running',%s,%s,%s,%s)
                """,
                (run_id, *expected[:-1], started_at_utc_naive, created_by),
            )
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise


def finish_semantic_run(
    connection: pymysql.connections.Connection,
    *,
    schema: str,
    run_id: str,
    calls_used: int,
    failed_batches: int,
    finished_at_utc_naive: str,
) -> None:
    """Close one run without reporting complete when any batch failed."""
    safe_schema = validated_stage_schema(schema)
    terminal_status = "complete" if failed_batches == 0 else "partial_failed"
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                    UPDATE `{safe_schema}`.`{RUN_TABLE}`
                    SET status=%s, calls_used=%s, failed_batches=%s, finished_at=%s
                    WHERE run_id=%s AND status='running'
                """,
                (terminal_status, calls_used, failed_batches, finished_at_utc_naive, run_id),
            )
            if int(cursor.rowcount) != 1:
                raise ImmutableResultConflict(
                    f"run completion CAS failed: affected_rows={int(cursor.rowcount)}, expected=1"
                )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def start_semantic_batch(
    connection: pymysql.connections.Connection,
    *,
    schema: str,
    run_id: str,
    batch: SemanticBatch,
    started_at_utc_naive: str,
) -> bool:
    """Persist a deterministic batch start and return whether work is required."""
    safe_schema = validated_stage_schema(schema)
    select_sql = f"""
        SELECT wave_no, scope_id, brand, occurrence_count, semantic_key_count,
               occurrence_sha256, status
        FROM `{safe_schema}`.`{BATCH_TABLE}`
        WHERE run_id=%s AND batch_id=%s
    """
    expected = (
        batch.wave_no,
        batch.scope_id,
        batch.brand,
        len(batch.occurrences),
        len({item.semantic_event_key_v1 for item in batch.occurrences}),
        batch.occurrence_sha256,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(select_sql, (run_id, batch.batch_id))
            row = cursor.fetchone()
            if row is not None:
                existing = (
                    int(row["wave_no"]),
                    str(row["scope_id"]),
                    str(row["brand"]),
                    int(row["occurrence_count"]),
                    int(row["semantic_key_count"]),
                    str(row["occurrence_sha256"]),
                )
                if existing != expected:
                    raise ImmutableResultConflict(
                        "IMMUTABLE_RESULT_CONFLICT: deterministic batch identity has different input"
                    )
                status = str(row["status"])
                if status == "complete":
                    connection.commit()
                    return False
                if status == "failed":
                    cursor.execute(
                        f"""
                            UPDATE `{safe_schema}`.`{BATCH_TABLE}`
                            SET status='running', calls_used=0, error_code=NULL,
                                error_message=NULL, started_at=%s, finished_at=NULL
                            WHERE run_id=%s AND batch_id=%s AND status='failed'
                        """,
                        (started_at_utc_naive, run_id, batch.batch_id),
                    )
                    if int(cursor.rowcount) != 1:
                        raise ImmutableResultConflict(
                            "IMMUTABLE_RESULT_CONFLICT: failed batch restart CAS failed"
                        )
                    connection.commit()
                    return True
                if status != "running":
                    raise ImmutableResultConflict(
                        f"IMMUTABLE_RESULT_CONFLICT: unsupported batch status {status!r}"
                    )
                connection.commit()
                return True
            cursor.execute(
                f"""
                    INSERT INTO `{safe_schema}`.`{BATCH_TABLE}`
                      (run_id, batch_id, wave_no, scope_id, brand, occurrence_count,
                       semantic_key_count, occurrence_sha256, status, started_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'running',%s)
                """,
                (run_id, batch.batch_id, *expected, started_at_utc_naive),
            )
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise


def mark_semantic_batch_complete(
    connection: pymysql.connections.Connection,
    *,
    schema: str,
    run_id: str,
    batch_id: str,
    calls_used: int,
    finished_at_utc_naive: str,
    diagnostic_code: str | None = None,
    diagnostic_message: str | None = None,
) -> None:
    """Close exactly one running batch after its semantic rows commit."""
    safe_schema = validated_stage_schema(schema)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                    UPDATE `{safe_schema}`.`{BATCH_TABLE}`
                    SET status='complete', calls_used=%s, error_code=%s,
                        error_message=%s, finished_at=%s
                    WHERE run_id=%s AND batch_id=%s AND status='running'
                """,
                (
                    calls_used,
                    diagnostic_code,
                    diagnostic_message,
                    finished_at_utc_naive,
                    run_id,
                    batch_id,
                ),
            )
            if int(cursor.rowcount) != 1:
                raise ImmutableResultConflict(
                    f"batch completion CAS failed: affected_rows={int(cursor.rowcount)}, expected=1"
                )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def record_semantic_batch_failure(
    connection: pymysql.connections.Connection,
    *,
    schema: str,
    run_id: str,
    batch_id: str,
    error_code: str,
    error_message: str,
    finished_at_utc_naive: str,
    calls_used: int = 0,
) -> None:
    """Durably close one running batch as failed without hiding the failure."""
    safe_schema = validated_stage_schema(schema)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                    UPDATE `{safe_schema}`.`{BATCH_TABLE}`
                    SET status='failed', calls_used=%s, error_code=%s,
                        error_message=%s, finished_at=%s
                    WHERE run_id=%s AND batch_id=%s AND status='running'
                """,
                (
                    calls_used,
                    error_code[:64],
                    error_message[:1000],
                    finished_at_utc_naive,
                    run_id,
                    batch_id,
                ),
            )
            if int(cursor.rowcount) != 1:
                raise ImmutableResultConflict(
                    f"batch failure CAS failed: affected_rows={int(cursor.rowcount)}, expected=1"
                )
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def load_semantic_batch_statuses(
    connection: pymysql.connections.Connection,
    *,
    schema: str,
    run_id: str,
    batch_ids: Sequence[str],
) -> dict[str, str]:
    """Read every requested batch state by the full run/batch primary key."""
    if not batch_ids:
        return {}
    safe_schema = validated_stage_schema(schema)
    placeholders = ",".join("%s" for _ in batch_ids)
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
                SELECT batch_id, status
                FROM `{safe_schema}`.`{BATCH_TABLE}`
                WHERE run_id=%s AND batch_id IN ({placeholders})
                ORDER BY batch_id
            """,
            (run_id, *batch_ids),
        )
        rows = cursor.fetchall()
    return {str(row["batch_id"]): str(row["status"]) for row in rows}


def load_semantic_batch_calls(
    connection: pymysql.connections.Connection,
    *,
    schema: str,
    run_id: str,
    batch_ids: Sequence[str],
) -> int:
    """Sum actual calls for the complete requested batch primary keys."""
    if not batch_ids:
        return 0
    safe_schema = validated_stage_schema(schema)
    placeholders = ",".join("%s" for _ in batch_ids)
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
                SELECT COALESCE(SUM(calls_used), 0) AS calls_used
                FROM `{safe_schema}`.`{BATCH_TABLE}`
                WHERE run_id=%s AND batch_id IN ({placeholders})
                  AND status='complete'
            """,
            (run_id, *batch_ids),
        )
        row = cursor.fetchone()
    return int(row["calls_used"])


def cas_active_release(
    connection: ConnectionLike,
    *,
    schema: str,
    pointer_name: str,
    expected_generation: int,
    expected_active_release_id: str | None,
    new_release_id: str,
    actor: str,
    now_utc_naive: str,
) -> int:
    """Advance one active pointer only when both identity and generation match."""
    safe_schema = validated_stage_schema(schema)
    sql = f"""
        UPDATE `{safe_schema}`.`{ACTIVE_RELEASE_TABLE}`
        SET active_release_id=%s,
            generation=generation+1,
            updated_at=%s,
            updated_by=%s
        WHERE pointer_name=%s
          AND generation=%s
          AND active_release_id <=> %s
    """
    with connection.cursor() as cursor:  # type: ignore[attr-defined]
        cursor.execute(
            sql,
            (new_release_id, now_utc_naive, actor, pointer_name, expected_generation, expected_active_release_id),
        )
        affected = int(cursor.rowcount)
    if affected != 1:
        connection.rollback()
        raise ReleaseCasConflict(f"release CAS failed: affected_rows={affected}, expected=1")
    connection.commit()
    return affected


def _insert_assignments(
    connection: pymysql.connections.Connection,
    *,
    schema: str,
    assignments: Sequence[SemanticAssignment],
    assigned_at_utc_naive: str,
) -> int:
    if not assignments:
        return 0
    affected = 0
    select_sql = f"""
        SELECT semantic_event_key_v1, scope_id, brand, topic_id, topic_set_version,
               prompt_version, batch_id
        FROM `{schema}`.`{ASSIGNMENT_TABLE}`
        WHERE semantic_event_key_v1=%s AND scope_id=%s
          AND topic_set_version=%s AND topic_id=%s
    """
    insert_sql = f"""
        INSERT INTO `{schema}`.`{ASSIGNMENT_TABLE}`
          (semantic_event_key_v1, scope_id, brand, topic_id, topic_set_version,
           prompt_version, assigned_at, batch_id)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
    """
    with connection.cursor() as cursor:
        for item in assignments:
            cursor.execute(
                select_sql,
                (item.semantic_event_key_v1, item.scope_id, item.topic_set_version, item.topic_id),
            )
            row = cursor.fetchone()
            if row is not None:
                existing = SemanticAssignment(
                    semantic_event_key_v1=str(row["semantic_event_key_v1"]),
                    scope_id=str(row["scope_id"]),
                    brand=str(row["brand"]),
                    topic_id=str(row["topic_id"]),
                    topic_set_version=str(row["topic_set_version"]),
                    prompt_version=str(row["prompt_version"]),
                    batch_id=str(row["batch_id"]),
                )
                assert_immutable_assignment_compatible(item, existing)
                continue
            cursor.execute(
                insert_sql,
                (
                    item.semantic_event_key_v1,
                    item.scope_id,
                    item.brand,
                    item.topic_id,
                    item.topic_set_version,
                    item.prompt_version,
                    assigned_at_utc_naive,
                    item.batch_id,
                ),
            )
            affected += int(cursor.rowcount)
    return affected


def _insert_statuses(
    connection: pymysql.connections.Connection,
    *,
    schema: str,
    statuses: Sequence[SemanticStatus],
    classified_at_utc_naive: str,
) -> int:
    if not statuses:
        return 0
    select_sql = f"""
        SELECT semantic_event_key_v1, scope_id, topic_set_version,
               classified_stage_generation_id, prompt_version, batch_id,
               status, assignment_count
        FROM `{schema}`.`{STATUS_TABLE}`
        WHERE semantic_event_key_v1=%s AND scope_id=%s AND topic_set_version=%s
    """
    insert_sql = f"""
        INSERT INTO `{schema}`.`{STATUS_TABLE}`
          (semantic_event_key_v1, scope_id, topic_set_version,
           classified_stage_generation_id, prompt_version, batch_id,
           status, assignment_count, classified_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """
    affected = 0
    with connection.cursor() as cursor:
        for item in statuses:
            cursor.execute(
                select_sql,
                (item.semantic_event_key_v1, item.scope_id, item.topic_set_version),
            )
            row = cursor.fetchone()
            if row is not None:
                existing = SemanticStatus(
                    semantic_event_key_v1=str(row["semantic_event_key_v1"]),
                    scope_id=str(row["scope_id"]),
                    topic_set_version=str(row["topic_set_version"]),
                    classified_stage_generation_id=str(row["classified_stage_generation_id"]),
                    prompt_version=str(row["prompt_version"]),
                    batch_id=str(row["batch_id"]),
                    status=str(row["status"]),
                    assignment_count=int(row["assignment_count"]),
                )
                refresh_audit = assert_immutable_status_compatible(item, existing)
                if refresh_audit:
                    cursor.execute(
                        f"""
                            UPDATE `{schema}`.`{STATUS_TABLE}`
                            SET classified_stage_generation_id=%s, batch_id=%s, classified_at=%s
                            WHERE semantic_event_key_v1=%s AND scope_id=%s
                              AND topic_set_version=%s AND classified_stage_generation_id=%s
                        """,
                        (
                            item.classified_stage_generation_id,
                            item.batch_id,
                            classified_at_utc_naive,
                            item.semantic_event_key_v1,
                            item.scope_id,
                            item.topic_set_version,
                            existing.classified_stage_generation_id,
                        ),
                    )
                    if int(cursor.rowcount) != 1:
                        raise ImmutableResultConflict(
                            "IMMUTABLE_RESULT_CONFLICT: semantic status audit CAS failed"
                        )
                    affected += 1
                continue
            try:
                cursor.execute(
                    insert_sql,
                    (
                        item.semantic_event_key_v1,
                        item.scope_id,
                        item.topic_set_version,
                        item.classified_stage_generation_id,
                        item.prompt_version,
                        item.batch_id,
                        item.status,
                        item.assignment_count,
                        classified_at_utc_naive,
                    ),
                )
            except pymysql.IntegrityError as exc:
                raise ImmutableResultConflict(
                    "IMMUTABLE_RESULT_CONFLICT: semantic status already exists; exact reuse must be prefiltered"
                ) from exc
            affected += int(cursor.rowcount)
    return affected
