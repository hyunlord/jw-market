#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "httpx2",
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
    TopicRubric,
    parse_assignment_response,
    row_topic_prompt,
)
from pipeline.scripts.analysis.brand_activity.auto_topic.row_topic_db import PreparedRun, apply_ddl, insert_assignments, prepare_run
from pipeline.scripts.analysis.brand_activity.auto_topic.row_topic_runner import AssignmentBatch, AssignmentChatClient, plan_batches
from pipeline.scripts.analysis.brand_activity.auto_topic.topic_store import validated_stage_schema


PROMPT_VERSION: Final = "row_topic_v1"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("apply-ddl", "dry-run", "execute"))
    parser.add_argument("--schema", default=SCHEMA)
    parser.add_argument("--topic-set-version", default="")
    parser.add_argument("--checkpoint", type=Path, default=Path("/tmp/row_topic_assignment_checkpoint.jsonl"))
    parser.add_argument("--log", type=Path, default=Path("/tmp/row_topic_assignment_execute_log.jsonl"))
    parser.add_argument("--batch-size", type=int, default=150)
    parser.add_argument("--max-calls", type=int, default=0)
    parser.add_argument("--base-url", default="https://jwai-dev.jwhealthcare.com")
    parser.add_argument("--serving-id", default="163")
    args = parser.parse_args()
    schema = validated_stage_schema(args.schema)
    connection = connect_mariadb(read_env_file())
    try:
        if args.mode == "apply-ddl":
            _print_json(apply_ddl(connection, schema=schema))
            return 0
        prepared = prepare_run(connection, schema=schema, topic_set_version=args.topic_set_version)
        summary = dry_summary(prepared, batch_size=args.batch_size, checkpoint_path=args.checkpoint)
        _print_json(summary)
        if args.mode == "dry-run":
            return 0
        client = AssignmentChatClient(base_url=args.base_url, token=_required_env("GENOS_BEARER_TOKEN"), serving_id=args.serving_id)
        result = execute(prepared, connection, client, schema=schema, batch_size=args.batch_size, max_calls=args.max_calls, checkpoint_path=args.checkpoint, log_path=args.log)
        _print_json(result)
        return 0
    finally:
        connection.close()


def dry_summary(prepared: PreparedRun, *, batch_size: int, checkpoint_path: Path) -> dict[str, JsonValue]:
    """Return the no-call estimate used as the cost gate."""
    plan = plan_batches(list(prepared.rows), batch_size=batch_size, prompt_version=PROMPT_VERSION, checkpoint_path=checkpoint_path)
    return {
        "mode": "dry-run",
        "topic_set_version": prepared.topic_set_version,
        "total_rows": plan.total_rows,
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
) -> dict[str, JsonValue]:
    """Classify all pending batches, inserting each successful batch before checkpointing."""
    plan = plan_batches(list(prepared.rows), batch_size=batch_size, prompt_version=PROMPT_VERSION, checkpoint_path=checkpoint_path)
    if max_calls and plan.estimated_calls > max_calls:
        raise AssignmentParseError(f"pending calls {plan.estimated_calls} exceed cap {max_calls}")
    calls_used = 0
    inserted = 0
    assignments_total = 0
    none_rows = 0
    for batch in plan.pending_batches:
        parsed = _classify_batch(client, prepared, batch, max_calls=max_calls, calls_used=calls_used)
        calls_used += parsed["calls"]
        assignments = parsed["assignments"]
        inserted += insert_assignments(connection, schema=schema, assignments=assignments)
        assignments_total += len(assignments)
        none_rows += len(batch.rows) - len({assignment.row_id for assignment in assignments})
        _append_jsonl(checkpoint_path, {"batch_id": batch.batch_id, "status": "ok", "row_count": len(batch.rows), "assignment_count": len(assignments)})
        _append_jsonl(log_path, {"batch_id": batch.batch_id, "row_count": len(batch.rows), "assignment_count": len(assignments), "calls_used": calls_used})
        print(json.dumps({"event": "batch_done", "batch_id": batch.batch_id, "calls_used": calls_used, "assignment_count": len(assignments)}, ensure_ascii=False), flush=True)
    return {
        "mode": "execute",
        "topic_set_version": prepared.topic_set_version,
        "pending_batches_before": len(plan.pending_batches),
        "calls_used": calls_used,
        "assignment_rows_inserted_or_updated": inserted,
        "assignments_total": assignments_total,
        "none_rows": none_rows,
        "checkpoint_path": str(checkpoint_path),
        "log_path": str(log_path),
    }


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
    return _classify_with_one_parse_retry(
        client,
        rubric,
        batch.rows,
        prepared.topic_set_version,
        batch.batch_id,
        max_calls=max_calls,
        calls_used=calls_used,
    )


def _classify_with_one_parse_retry(
    client: AssignmentChatClient,
    rubric: tuple[TopicRubric, ...],
    rows: tuple[AssignmentInputRow, ...],
    topic_set_version: str,
    batch_id: str,
    *,
    max_calls: int,
    calls_used: int,
) -> dict[str, JsonValue]:
    known_topic_ids = {topic.topic_id for topic in rubric}
    last_error = ""
    for attempt in (1, 2):
        if max_calls and calls_used + attempt > max_calls:
            raise AssignmentParseError(f"call cap {max_calls} reached before {batch_id}")
        content, _usage, _latency_ms = client.classify(row_topic_prompt(rubric, rows))
        try:
            assignments = parse_assignment_response(content, list(rows), known_topic_ids, topic_set_version, batch_id)
            return {"assignments": assignments, "calls": attempt}
        except AssignmentParseError as exc:
            last_error = str(exc)
            if attempt == 2:
                raise
    raise AssignmentParseError(last_error)


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
