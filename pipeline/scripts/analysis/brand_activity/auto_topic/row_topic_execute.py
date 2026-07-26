#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "pymysql",
# ]
# ///
"""Execute row-level topic assignment against the measured topic mart rubric."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Final

REPO_ROOT = Path(__file__).resolve().parents[5]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pymysql

from pipeline.scripts.analysis.brand_activity.auto_topic.data_source import SCHEMA, connect_mariadb, read_env_file
from pipeline.scripts.analysis.brand_activity.auto_topic.models import JsonValue
from pipeline.scripts.analysis.brand_activity.auto_topic.row_topic_assignment import (
    AssignmentInputRow,
    AssignmentParseError,
    RowTopicAssignment,
    TopicRubric,
    parse_assignment_response_allow_missing,
    parse_assignment_response,
    row_topic_prompt,
)
from pipeline.scripts.analysis.brand_activity.auto_topic.row_topic_db import (
    STATUS_CLASSIFIED,
    STATUS_UNRESOLVED_MISSING,
    PreparedRun,
    RowTopicAssignmentStatus,
    apply_ddl,
    insert_assignment_batch,
    insert_assignments,
    load_pending_rows,
    prepare_run,
)
from pipeline.scripts.analysis.brand_activity.auto_topic.row_topic_runner import AssignmentBatch, AssignmentChatClient, plan_batches
from pipeline.scripts.analysis.brand_activity.auto_topic.topic_assignment_handoff_db import (
    assignment_gap_json,
    mark_assignment_running,
    reconcile_assignment_handoff,
    require_axis_handoff,
)
from pipeline.scripts.analysis.brand_activity.auto_topic.topic_store import validated_stage_schema


PROMPT_VERSION: Final = "row_topic_v1"
PENDING_SOURCE_FILE: Final = "file"
PENDING_SOURCE_DB: Final = "db"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode",
        choices=("apply-ddl", "dry-run", "execute", "reconcile"),
    )
    parser.add_argument("--schema", default=SCHEMA)
    parser.add_argument("--topic-set-version", default="")
    parser.add_argument("--checkpoint", type=Path, default=Path("/tmp/row_topic_assignment_checkpoint.jsonl"))
    parser.add_argument("--log", type=Path, default=Path("/tmp/row_topic_assignment_execute_log.jsonl"))
    parser.add_argument("--batch-size", type=int, default=150)
    parser.add_argument("--max-calls", type=int, default=0)
    parser.add_argument("--base-url", default="https://jwai-dev.jwhealthcare.com")
    parser.add_argument("--serving-id", default="163")
    parser.add_argument("--pending-source", choices=(PENDING_SOURCE_FILE, PENDING_SOURCE_DB), default=PENDING_SOURCE_FILE)
    parser.add_argument("--retry-unresolved", action="store_true")
    args = parser.parse_args()
    schema = validated_stage_schema(args.schema)
    connection = connect_mariadb(read_env_file())
    try:
        if args.mode == "apply-ddl":
            _print_json(apply_ddl(connection, schema=schema))
            return 0
        prepared = prepare_receipted_run(
            connection,
            schema=schema,
            topic_set_version=args.topic_set_version,
        )
        if args.mode == "reconcile":
            gap = reconcile_assignment_handoff(
                connection,
                schema=schema,
                prepared=prepared,
            )
            payload = assignment_gap_json(gap)
            _print_json(payload)
            return 0 if gap.complete else 2
        summary = dry_summary(
            prepared,
            connection,
            schema=schema,
            batch_size=args.batch_size,
            checkpoint_path=args.checkpoint,
            pending_source=args.pending_source,
            retry_unresolved=args.retry_unresolved,
        )
        _print_json(summary)
        if args.mode == "dry-run":
            return 0
        mark_assignment_running(
            connection,
            schema=schema,
            run_id=prepared.topic_set_version,
        )
        client = AssignmentChatClient(base_url=args.base_url, token=_required_env("GENOS_BEARER_TOKEN"), serving_id=args.serving_id)
        result = execute(
            prepared,
            connection,
            client,
            schema=schema,
            batch_size=args.batch_size,
            max_calls=args.max_calls,
            checkpoint_path=args.checkpoint,
            log_path=args.log,
            pending_source=args.pending_source,
            retry_unresolved=args.retry_unresolved,
        )
        gap = reconcile_assignment_handoff(
            connection,
            schema=schema,
            prepared=prepared,
        )
        result["reconciliation"] = assignment_gap_json(gap)
        _print_json(result)
        return 0 if gap.complete else 2
    finally:
        connection.close()


def prepare_receipted_run(
    connection: pymysql.connections.Connection,
    *,
    schema: str,
    topic_set_version: str,
) -> PreparedRun:
    """Resolve only an explicit run whose exact axis receipt is complete."""
    if not topic_set_version:
        raise AssignmentParseError(
            "explicit topic-set-version is required; latest-run inference is disabled"
        )
    prepared = prepare_run(
        connection,
        schema=schema,
        topic_set_version=topic_set_version,
    )
    require_axis_handoff(
        connection,
        schema=schema,
        prepared=prepared,
    )
    return prepared


def dry_summary(
    prepared: PreparedRun,
    connection: pymysql.connections.Connection,
    *,
    schema: str,
    batch_size: int,
    checkpoint_path: Path,
    pending_source: str = PENDING_SOURCE_FILE,
    retry_unresolved: bool = False,
) -> dict[str, JsonValue]:
    """Return the no-call estimate used as the cost gate."""
    rows = _pending_rows_for_source(
        prepared,
        connection,
        schema=schema,
        pending_source=pending_source,
        retry_unresolved=retry_unresolved,
    )
    plan = plan_batches(
        rows,
        batch_size=batch_size,
        prompt_version=PROMPT_VERSION,
        checkpoint_path=checkpoint_path,
        ignore_checkpoint=pending_source == PENDING_SOURCE_DB,
    )
    return {
        "mode": "dry-run",
        "topic_set_version": prepared.topic_set_version,
        "pending_source": pending_source,
        "total_rows": len(prepared.rows),
        "pending_rows": plan.total_rows,
        "scope_brand_pairs": plan.total_scope_brand_pairs,
        "total_batches": plan.total_batches,
        "pending_batches": len(plan.pending_batches),
        "estimated_calls": plan.estimated_calls,
        "estimated_usd": plan.estimated_usd,
        "checkpoint_path": str(checkpoint_path),
    }


def execute(
    prepared: PreparedRun,
    connection: pymysql.connections.Connection,
    client: AssignmentChatClient,
    *,
    schema: str,
    batch_size: int,
    max_calls: int,
    checkpoint_path: Path,
    log_path: Path,
    pending_source: str = PENDING_SOURCE_FILE,
    retry_unresolved: bool = False,
) -> dict[str, JsonValue]:
    """Classify all pending batches, inserting each successful batch before checkpointing."""
    rows = _pending_rows_for_source(
        prepared,
        connection,
        schema=schema,
        pending_source=pending_source,
        retry_unresolved=retry_unresolved,
    )
    plan = plan_batches(
        rows,
        batch_size=batch_size,
        prompt_version=PROMPT_VERSION,
        checkpoint_path=checkpoint_path,
        ignore_checkpoint=pending_source == PENDING_SOURCE_DB,
    )
    if max_calls and plan.estimated_calls > max_calls:
        raise AssignmentParseError(f"pending calls {plan.estimated_calls} exceed cap {max_calls}")
    calls_used = 0
    inserted = 0
    assignments_total = 0
    none_rows = 0
    fallback_calls = 0
    missing_rows_total = 0
    unresolved_missing: list[int] = []
    for batch in plan.pending_batches:
        parsed = _classify_batch(client, prepared, batch, max_calls=max_calls, calls_used=calls_used)
        calls_used += parsed["calls"]
        fallback_calls += int(parsed.get("fallback_calls") or 0)
        missing_row_ids = [int(row_id) for row_id in parsed.get("missing_row_ids", [])]
        missing_rows_total += len(missing_row_ids)
        unresolved_missing.extend(missing_row_ids)
        assignments = parsed["assignments"]
        if pending_source == PENDING_SOURCE_DB:
            statuses = _status_rows_for_batch(
                batch.rows,
                assignments,
                missing_row_ids=missing_row_ids,
                topic_set_version=prepared.topic_set_version,
                batch_id=batch.batch_id,
            )
            inserted_rows, _status_rows = insert_assignment_batch(connection, schema=schema, assignments=assignments, statuses=statuses)
            inserted += inserted_rows
        else:
            inserted += insert_assignments(connection, schema=schema, assignments=assignments)
        assignments_total += len(assignments)
        none_rows += len(batch.rows) - len({assignment.row_id for assignment in assignments})
        checkpoint_payload = {
            "batch_id": batch.batch_id,
            "status": "ok",
            "row_count": len(batch.rows),
            "assignment_count": len(assignments),
            "fallback_calls": int(parsed.get("fallback_calls") or 0),
            "missing_row_ids": missing_row_ids,
        }
        _append_jsonl(checkpoint_path, checkpoint_payload)
        _append_jsonl(log_path, {**checkpoint_payload, "calls_used": calls_used})
        print(
            json.dumps(
                {
                    "event": "batch_done",
                    "batch_id": batch.batch_id,
                    "calls_used": calls_used,
                    "assignment_count": len(assignments),
                    "fallback_calls": int(parsed.get("fallback_calls") or 0),
                    "missing_row_count": len(missing_row_ids),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    return {
        "mode": "execute",
        "topic_set_version": prepared.topic_set_version,
        "pending_source": pending_source,
        "pending_rows_before": plan.total_rows,
        "pending_batches_before": len(plan.pending_batches),
        "calls_used": calls_used,
        "assignment_rows_inserted_or_updated": inserted,
        "assignments_total": assignments_total,
        "none_rows": none_rows,
        "fallback_calls": fallback_calls,
        "missing_rows_total": missing_rows_total,
        "missing_row_ids": unresolved_missing[:100],
        "checkpoint_path": str(checkpoint_path),
        "log_path": str(log_path),
    }


def _pending_rows_for_source(
    prepared: PreparedRun,
    connection: pymysql.connections.Connection,
    *,
    schema: str,
    pending_source: str,
    retry_unresolved: bool,
) -> list[AssignmentInputRow]:
    if pending_source == PENDING_SOURCE_DB:
        return load_pending_rows(
            connection,
            schema=schema,
            topic_set_version=prepared.topic_set_version,
            rows=list(prepared.rows),
            retry_unresolved=retry_unresolved,
        )
    return list(prepared.rows)


def _status_rows_for_batch(
    rows: tuple[AssignmentInputRow, ...],
    assignments: list[RowTopicAssignment],
    *,
    missing_row_ids: list[int],
    topic_set_version: str,
    batch_id: str,
) -> list[RowTopicAssignmentStatus]:
    assignment_counts: dict[int, int] = {}
    for assignment in assignments:
        assignment_counts[assignment.row_id] = assignment_counts.get(assignment.row_id, 0) + 1
    missing = set(missing_row_ids)
    statuses: list[RowTopicAssignmentStatus] = []
    for row in rows:
        status = STATUS_UNRESOLVED_MISSING if row.row_id in missing else STATUS_CLASSIFIED
        statuses.append(
            RowTopicAssignmentStatus(
                topic_set_version=topic_set_version,
                scope_id=row.scope_id,
                row_id=row.row_id,
                stage_row_sha256=row.stage_row_sha256,
                prompt_version=PROMPT_VERSION,
                batch_id=batch_id,
                status=status,
                assignment_count=assignment_counts.get(row.row_id, 0),
            )
        )
    return statuses


def _classify_batch(
    client: AssignmentChatClient,
    prepared: PreparedRun,
    batch: AssignmentBatch,
    *,
    max_calls: int,
    calls_used: int,
) -> dict[str, JsonValue]:
    rubric = prepared.rubrics.get((batch.rows[0].scope_id, batch.rows[0].brand))
    if not rubric:
        raise AssignmentParseError(f"missing rubric for {batch.rows[0].scope_id} / {batch.rows[0].brand}")
    return _classify_with_missing_fallback(
        client,
        rubric,
        batch.rows,
        prepared.topic_set_version,
        batch.batch_id,
        max_calls=max_calls,
        calls_used=calls_used,
    )


def _classify_with_missing_fallback(
    client: AssignmentChatClient,
    rubric: tuple[TopicRubric, ...],
    rows: tuple[AssignmentInputRow, ...],
    topic_set_version: str,
    batch_id: str,
    *,
    max_calls: int,
    calls_used: int,
) -> dict[str, JsonValue]:
    primary = _classify_with_one_parse_retry(
        client,
        rubric,
        rows,
        topic_set_version,
        batch_id,
        max_calls=max_calls,
        calls_used=calls_used,
        allow_missing=True,
    )
    assignments = list(primary["assignments"])
    calls = int(primary["calls"])
    missing_rows = _rows_by_id(rows, [int(row_id) for row_id in primary.get("missing_row_ids", [])])
    fallback_calls = 0
    unresolved: list[int] = []
    if missing_rows:
        fallback = _classify_missing_chunks(
            client,
            rubric,
            missing_rows,
            topic_set_version,
            batch_id,
            max_calls=max_calls,
            calls_used=calls_used + calls,
            chunk_size=10,
        )
        assignments.extend(fallback["assignments"])
        fallback_calls += int(fallback["calls"])
        calls += int(fallback["calls"])
        unresolved = [int(row_id) for row_id in fallback.get("missing_row_ids", [])]
    return {
        "assignments": assignments,
        "calls": calls,
        "fallback_calls": fallback_calls,
        "missing_row_ids": unresolved,
    }


def _classify_missing_chunks(
    client: AssignmentChatClient,
    rubric: tuple[TopicRubric, ...],
    rows: tuple[AssignmentInputRow, ...],
    topic_set_version: str,
    batch_id: str,
    *,
    max_calls: int,
    calls_used: int,
    chunk_size: int,
) -> dict[str, JsonValue]:
    assignments = []
    calls = 0
    unresolved: list[int] = []
    for index, offset in enumerate(range(0, len(rows), chunk_size), start=1):
        chunk = tuple(rows[offset : offset + chunk_size])
        parsed = _classify_with_one_parse_retry(
            client,
            rubric,
            chunk,
            topic_set_version,
            batch_id,
            max_calls=max_calls,
            calls_used=calls_used + calls,
            allow_missing=True,
        )
        assignments.extend(parsed["assignments"])
        calls += int(parsed["calls"])
        missing_rows = _rows_by_id(chunk, [int(row_id) for row_id in parsed.get("missing_row_ids", [])])
        if not missing_rows:
            continue
        if chunk_size > 3:
            smaller = _classify_missing_chunks(
                client,
                rubric,
                missing_rows,
                topic_set_version,
                batch_id,
                max_calls=max_calls,
                calls_used=calls_used + calls,
                chunk_size=3,
            )
            assignments.extend(smaller["assignments"])
            calls += int(smaller["calls"])
            unresolved.extend(int(row_id) for row_id in smaller.get("missing_row_ids", []))
        else:
            unresolved.extend(row.row_id for row in missing_rows)
    return {"assignments": assignments, "calls": calls, "missing_row_ids": unresolved}


def _classify_with_one_parse_retry(
    client: AssignmentChatClient,
    rubric: tuple[TopicRubric, ...],
    rows: tuple[AssignmentInputRow, ...],
    topic_set_version: str,
    batch_id: str,
    *,
    max_calls: int,
    calls_used: int,
    allow_missing: bool = False,
) -> dict[str, JsonValue]:
    known_topic_ids = {topic.topic_id for topic in rubric}
    last_error = ""
    for attempt in (1, 2):
        if max_calls and calls_used + attempt > max_calls:
            raise AssignmentParseError(f"call cap {max_calls} reached before {batch_id}")
        content, _usage, _latency_ms = client.classify(row_topic_prompt(rubric, rows))
        try:
            if allow_missing:
                parsed = parse_assignment_response_allow_missing(content, list(rows), known_topic_ids, topic_set_version, batch_id)
                return {"assignments": parsed.assignments, "missing_row_ids": list(parsed.missing_row_ids), "calls": attempt}
            assignments = parse_assignment_response(content, list(rows), known_topic_ids, topic_set_version, batch_id)
            return {"assignments": assignments, "missing_row_ids": [], "calls": attempt}
        except AssignmentParseError as exc:
            last_error = str(exc)
            if attempt == 2:
                raise
    raise AssignmentParseError(last_error)


def _rows_by_id(rows: tuple[AssignmentInputRow, ...], row_ids: list[int]) -> tuple[AssignmentInputRow, ...]:
    by_id = {row.row_id: row for row in rows}
    return tuple(by_id[row_id] for row_id in row_ids if row_id in by_id)


def _apply_ddl(connection: pymysql.connections.Connection, *, schema: str) -> int:
    if _table_exists(connection, schema=schema, table=ASSIGNMENT_TABLE):
        raise AssignmentParseError(f"{schema}.{ASSIGNMENT_TABLE} already exists; refusing to overwrite")
    with connection.cursor() as cursor:
        cursor.execute(assignment_table_ddl(schema))
        cursor.execute(compatible_share_view_sql(schema))
def _append_jsonl(path: Path, payload: dict[str, JsonValue]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def _required_env(name: str) -> str:
    import os

    value = os.environ.get(name, "")
    if not value:
        raise AssignmentParseError(f"{name} is not set")
    return value


def _print_json(payload: dict[str, JsonValue]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
