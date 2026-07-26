from __future__ import annotations

from dataclasses import asdict
import json
from typing import Final

import pymysql

from .models import JsonValue
from .row_topic_db import (
    ASSIGNMENT_STATUS_TABLE,
    ASSIGNMENT_TABLE,
    PreparedRun,
    load_assignment_rows,
    load_scope_rubrics,
)
from .topic_assignment_handoff import (
    ASSIGNMENT_COMPLETE,
    ASSIGNMENT_GAP,
    ASSIGNMENT_PENDING,
    ASSIGNMENT_RUNNING,
    AXIS_COMPLETE,
    AssignmentGap,
    AssignmentHandoffReceipt,
    AssignmentStatusSnapshot,
    HandoffBlockedError,
    TopicScopeSnapshot,
    evaluate_assignment_gap,
    evaluate_axis_completion,
    population_identity,
    require_assignment_ready,
    scope_identity,
)
from .topic_store import validated_stage_schema


HANDOFF_TABLE: Final = "mart_brand_activity_assignment_handoff"
STAGING_HANDOFF_TABLE: Final = "mart_brand_activity_assignment_handoff_staging"


def handoff_table_ddl(schema: str, table: str = HANDOFF_TABLE) -> str:
    """Return durable axis-to-assignment handoff DDL."""
    safe_schema = validated_stage_schema(schema)
    return f"""
CREATE TABLE IF NOT EXISTS `{safe_schema}`.`{table}` (
  run_id VARCHAR(160) NOT NULL,
  target_mode VARCHAR(32) NOT NULL,
  input_fingerprint CHAR(64) NOT NULL,
  expected_scope_count INT NOT NULL,
  stored_scope_count INT NOT NULL,
  scope_identity_sha256 CHAR(64) NOT NULL,
  assignment_population_count BIGINT NOT NULL,
  assignment_population_sha256 CHAR(64) NOT NULL,
  axis_status VARCHAR(32) NOT NULL,
  assignment_status VARCHAR(32) NOT NULL,
  last_error VARCHAR(512) NOT NULL DEFAULT '',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (run_id),
  KEY idx_topic_assignment_handoff_pending (axis_status, assignment_status, created_at, run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
""".strip()


def ensure_handoff_table(
    connection: pymysql.connections.Connection,
    *,
    schema: str,
    table: str = HANDOFF_TABLE,
) -> None:
    """Create the durable handoff table without changing scheduler resources."""
    with connection.cursor() as cursor:
        cursor.execute(handoff_table_ddl(schema, table))


def pending_handoff_query(schema: str, table: str = HANDOFF_TABLE) -> str:
    """Return the exact pending-run reconciliation query."""
    safe_schema = validated_stage_schema(schema)
    return f"""
SELECT run_id
FROM `{safe_schema}`.`{table}`
WHERE axis_status='complete'
  AND assignment_status IN ('pending','running','gap')
ORDER BY created_at, run_id
""".strip()


def record_axis_handoff(
    connection: pymysql.connections.Connection,
    *,
    schema: str,
    handoff_table: str,
    topics_table: str,
    run_id: str,
    target_mode: str,
    input_fingerprint: str,
    expected_scopes: tuple[TopicScopeSnapshot, ...],
) -> AssignmentHandoffReceipt:
    """Persist a pending receipt only after exact scope and stage-row readback."""
    safe_schema = validated_stage_schema(schema)
    expected = scope_identity(expected_scopes)
    stored_scopes = _stored_scope_snapshots(
        connection,
        schema=safe_schema,
        topics_table=topics_table,
        run_id=run_id,
    )
    stored = scope_identity(stored_scopes)
    completion = evaluate_axis_completion(expected, stored)
    empty_population = population_identity(())
    if completion.axis_status != AXIS_COMPLETE:
        receipt = AssignmentHandoffReceipt(
            run_id=run_id,
            target_mode=target_mode,
            input_fingerprint=input_fingerprint,
            expected_scope_count=expected.count,
            stored_scope_count=stored.count,
            scope_identity_sha256=expected.sha256,
            assignment_population_count=empty_population.count,
            assignment_population_sha256=empty_population.sha256,
            axis_status=completion.axis_status,
            assignment_status=completion.assignment_status,
        )
        _upsert_receipt(
            connection,
            schema=safe_schema,
            table=handoff_table,
            receipt=receipt,
            last_error="stored topic scopes do not exactly match generated scopes",
        )
        connection.commit()
        return receipt

    scopes = load_scope_rubrics(
        connection,
        schema=safe_schema,
        run_id=run_id,
    )
    rows = tuple(
        load_assignment_rows(
            connection,
            schema=safe_schema,
            scopes=scopes,
        )
    )
    population = population_identity(rows)
    receipt = AssignmentHandoffReceipt(
        run_id=run_id,
        target_mode=target_mode,
        input_fingerprint=input_fingerprint,
        expected_scope_count=expected.count,
        stored_scope_count=stored.count,
        scope_identity_sha256=stored.sha256,
        assignment_population_count=population.count,
        assignment_population_sha256=population.sha256,
        axis_status=AXIS_COMPLETE,
        assignment_status=ASSIGNMENT_PENDING,
    )
    _upsert_receipt(
        connection,
        schema=safe_schema,
        table=handoff_table,
        receipt=receipt,
        last_error="",
    )
    connection.commit()
    return receipt


def load_handoff_receipt(
    connection: pymysql.connections.Connection,
    *,
    schema: str,
    run_id: str,
    table: str = HANDOFF_TABLE,
) -> AssignmentHandoffReceipt | None:
    """Load one exact handoff receipt by run id."""
    safe_schema = validated_stage_schema(schema)
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
SELECT run_id, target_mode, input_fingerprint, expected_scope_count,
       stored_scope_count, scope_identity_sha256, assignment_population_count,
       assignment_population_sha256, axis_status, assignment_status
FROM `{safe_schema}`.`{table}`
WHERE run_id=%s
""",
            (run_id,),
        )
        row = cursor.fetchone()
    return _receipt_from_row(row) if row else None


def list_pending_handoff_run_ids(
    connection: pymysql.connections.Connection,
    *,
    schema: str,
    table: str = HANDOFF_TABLE,
) -> tuple[str, ...]:
    """Return every durable pending run for reconciliation."""
    with connection.cursor() as cursor:
        cursor.execute(pending_handoff_query(schema, table))
        rows = cursor.fetchall()
    return tuple(str(row["run_id"]) for row in rows)


def require_axis_handoff(
    connection: pymysql.connections.Connection,
    *,
    schema: str,
    prepared: PreparedRun,
    table: str = HANDOFF_TABLE,
) -> AssignmentHandoffReceipt:
    """Verify the persisted receipt against the current exact population."""
    receipt = load_handoff_receipt(
        connection,
        schema=schema,
        run_id=prepared.topic_set_version,
        table=table,
    )
    require_assignment_ready(receipt, prepared.rows)
    if receipt is None:
        raise HandoffBlockedError("assignment handoff receipt is missing")
    return receipt


def reconcile_assignment_handoff(
    connection: pymysql.connections.Connection,
    *,
    schema: str,
    prepared: PreparedRun,
    table: str = HANDOFF_TABLE,
) -> AssignmentGap:
    """Reconcile durable status and assignment rows for one pending receipt."""
    receipt = require_axis_handoff(
        connection,
        schema=schema,
        prepared=prepared,
        table=table,
    )
    statuses = _load_status_snapshots(
        connection,
        schema=schema,
        run_id=receipt.run_id,
    )
    assignment_scope_counts = _load_assignment_scope_counts(
        connection,
        schema=schema,
        run_id=receipt.run_id,
    )
    gap = evaluate_assignment_gap(
        prepared.rows,
        statuses,
        assignment_scope_counts=assignment_scope_counts,
    )
    assignment_status = ASSIGNMENT_COMPLETE if gap.complete else ASSIGNMENT_GAP
    _update_assignment_status(
        connection,
        schema=schema,
        table=table,
        run_id=receipt.run_id,
        assignment_status=assignment_status,
        last_error=_gap_message(gap),
    )
    connection.commit()
    return gap


def mark_assignment_running(
    connection: pymysql.connections.Connection,
    *,
    schema: str,
    run_id: str,
    table: str = HANDOFF_TABLE,
) -> None:
    """Mark a pending exact receipt running while keeping it reconcilable."""
    _update_assignment_status(
        connection,
        schema=schema,
        table=table,
        run_id=run_id,
        assignment_status=ASSIGNMENT_RUNNING,
        last_error="",
    )
    connection.commit()


def handoff_json(receipt: AssignmentHandoffReceipt) -> dict[str, JsonValue]:
    """Serialize a handoff receipt for audit output."""
    return {
        key: value
        for key, value in asdict(receipt).items()
        if isinstance(value, str | int | float | bool) or value is None
    }


def assignment_gap_json(gap: AssignmentGap) -> dict[str, JsonValue]:
    """Serialize exact reconciliation evidence."""
    return {
        "complete": gap.complete,
        "expected_row_count": gap.expected_row_count,
        "status_row_count": gap.status_row_count,
        "missing_row_ids": list(gap.missing_row_ids),
        "hash_mismatch_row_ids": list(gap.hash_mismatch_row_ids),
        "zero_assignment_scope_ids": list(gap.zero_assignment_scope_ids),
    }


def _stored_scope_snapshots(
    connection: pymysql.connections.Connection,
    *,
    schema: str,
    topics_table: str,
    run_id: str,
) -> tuple[TopicScopeSnapshot, ...]:
    sql = f"""
SELECT scope_id, display_name, atc4_values, quality_grade, source_row_count, payload
FROM `{schema}`.`{topics_table}`
WHERE run_id=%s
ORDER BY scope_id
"""
    with connection.cursor() as cursor:
        cursor.execute(sql, (run_id,))
        rows = cursor.fetchall()
    return tuple(
        TopicScopeSnapshot(
            scope_id=str(row["scope_id"]),
            display_name=str(row["display_name"]),
            atc4_values=tuple(_json_texts(row["atc4_values"])),
            quality_grade=str(row["quality_grade"]),
            source_row_count=int(row["source_row_count"]),
            payload=_json_object(row["payload"]),
        )
        for row in rows
    )


def _upsert_receipt(
    connection: pymysql.connections.Connection,
    *,
    schema: str,
    table: str,
    receipt: AssignmentHandoffReceipt,
    last_error: str,
) -> None:
    ensure_handoff_table(connection, schema=schema, table=table)
    sql = f"""
INSERT INTO `{schema}`.`{table}`
(run_id, target_mode, input_fingerprint, expected_scope_count, stored_scope_count,
 scope_identity_sha256, assignment_population_count, assignment_population_sha256,
 axis_status, assignment_status, last_error)
VALUES ({", ".join(["%s"] * 11)})
ON DUPLICATE KEY UPDATE
  target_mode=VALUES(target_mode),
  input_fingerprint=VALUES(input_fingerprint),
  expected_scope_count=VALUES(expected_scope_count),
  stored_scope_count=VALUES(stored_scope_count),
  scope_identity_sha256=VALUES(scope_identity_sha256),
  assignment_population_count=VALUES(assignment_population_count),
  assignment_population_sha256=VALUES(assignment_population_sha256),
  axis_status=VALUES(axis_status),
  assignment_status=VALUES(assignment_status),
  last_error=VALUES(last_error)
"""
    values = (
        receipt.run_id,
        receipt.target_mode,
        receipt.input_fingerprint,
        receipt.expected_scope_count,
        receipt.stored_scope_count,
        receipt.scope_identity_sha256,
        receipt.assignment_population_count,
        receipt.assignment_population_sha256,
        receipt.axis_status,
        receipt.assignment_status,
        last_error[:512],
    )
    with connection.cursor() as cursor:
        cursor.execute(sql, values)


def _load_status_snapshots(
    connection: pymysql.connections.Connection,
    *,
    schema: str,
    run_id: str,
) -> tuple[AssignmentStatusSnapshot, ...]:
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
SELECT scope_id, row_id, stage_row_sha256, assignment_count
FROM `{schema}`.`{ASSIGNMENT_STATUS_TABLE}`
WHERE topic_set_version=%s
ORDER BY scope_id, row_id
""",
            (run_id,),
        )
        rows = cursor.fetchall()
    return tuple(
        AssignmentStatusSnapshot(
            scope_id=str(row["scope_id"]),
            row_id=int(row["row_id"]),
            stage_row_sha256=str(row["stage_row_sha256"]),
            assignment_count=int(row["assignment_count"]),
        )
        for row in rows
    )


def _load_assignment_scope_counts(
    connection: pymysql.connections.Connection,
    *,
    schema: str,
    run_id: str,
) -> dict[str, int]:
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
SELECT scope_id, COUNT(*) AS assignment_count
FROM `{schema}`.`{ASSIGNMENT_TABLE}`
WHERE topic_set_version=%s
GROUP BY scope_id
""",
            (run_id,),
        )
        rows = cursor.fetchall()
    return {
        str(row["scope_id"]): int(row["assignment_count"])
        for row in rows
    }


def _update_assignment_status(
    connection: pymysql.connections.Connection,
    *,
    schema: str,
    table: str,
    run_id: str,
    assignment_status: str,
    last_error: str,
) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
UPDATE `{schema}`.`{table}`
SET assignment_status=%s, last_error=%s
WHERE run_id=%s AND axis_status=%s
""",
            (assignment_status, last_error[:512], run_id, AXIS_COMPLETE),
        )


def _receipt_from_row(row: dict[str, JsonValue]) -> AssignmentHandoffReceipt:
    return AssignmentHandoffReceipt(
        run_id=str(row["run_id"]),
        target_mode=str(row["target_mode"]),
        input_fingerprint=str(row["input_fingerprint"]),
        expected_scope_count=int(row["expected_scope_count"]),
        stored_scope_count=int(row["stored_scope_count"]),
        scope_identity_sha256=str(row["scope_identity_sha256"]),
        assignment_population_count=int(row["assignment_population_count"]),
        assignment_population_sha256=str(row["assignment_population_sha256"]),
        axis_status=str(row["axis_status"]),
        assignment_status=str(row["assignment_status"]),
    )


def _gap_message(gap: AssignmentGap) -> str:
    if gap.complete:
        return ""
    return (
        f"missing={len(gap.missing_row_ids)};"
        f"hash_mismatch={len(gap.hash_mismatch_row_ids)};"
        f"zero_assignment_scopes={len(gap.zero_assignment_scope_ids)}"
    )


def _json_object(value: JsonValue) -> dict[str, JsonValue]:
    if isinstance(value, str):
        loaded = json.loads(value)
        return loaded if isinstance(loaded, dict) else {}
    return value if isinstance(value, dict) else {}


def _json_texts(value: JsonValue) -> list[str]:
    if isinstance(value, str):
        loaded = json.loads(value)
    else:
        loaded = value
    if not isinstance(loaded, list):
        return []
    return [str(item) for item in loaded if isinstance(item, str)]
