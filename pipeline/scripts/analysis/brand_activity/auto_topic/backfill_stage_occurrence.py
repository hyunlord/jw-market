#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "openpyxl",
#     "pymysql",
# ]
# ///
from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Final, cast

REPO_ROOT = Path(__file__).resolve().parents[5]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pymysql

from pipeline.scripts.analysis.brand_activity.auto_topic.data_source import connect_mariadb, read_env_file
from pipeline.scripts.analysis.brand_activity.auto_topic.models import JsonValue
from pipeline.scripts.analysis.brand_activity.auto_topic.row_topic_identity import (
    SEMANTIC_FIELD_NAMES,
    SemanticEventFields,
    semantic_event_key_v1,
    stage_generation_id,
)
from pipeline.scripts.analysis.brand_activity.auto_topic.topic_store import validated_stage_schema


SOURCE_TABLE: Final = "km_keyword_event_stage"
TARGET_TABLE: Final = "row_topic_stage_occurrence_v1"
PROVENANCE_COLUMNS: Final[tuple[str, ...]] = (
    "source_file",
    "source_sheet",
    "source_row_no",
    "source_file_sha256",
    "stage_row_sha256",
)


def _stage_rows(connection: pymysql.connections.Connection, *, schema: str) -> list[dict[str, object]]:
    columns = ("id", *SEMANTIC_FIELD_NAMES, *PROVENANCE_COLUMNS)
    sql = f"SELECT {', '.join(columns)} FROM `{schema}`.`{SOURCE_TABLE}` ORDER BY id"
    with connection.cursor() as cursor:
        cursor.execute("START TRANSACTION READ ONLY")
        cursor.execute(sql)
        rows = cast(list[dict[str, object]], cursor.fetchall())
        cursor.execute("COMMIT")
    return rows


def _snapshot_fingerprint(rows: Sequence[dict[str, object]]) -> str:
    return stage_generation_id((int(row["id"]), str(row["stage_row_sha256"])) for row in rows)


def _current_snapshot(connection: pymysql.connections.Connection, *, schema: str) -> tuple[int, str]:
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT id, stage_row_sha256 FROM `{schema}`.`{SOURCE_TABLE}` ORDER BY id"
        )
        pairs = tuple((int(row["id"]), str(row["stage_row_sha256"])) for row in cursor.fetchall())
    return len(pairs), stage_generation_id(pairs)


def _semantic_fields(row: dict[str, object]) -> SemanticEventFields:
    return cast(SemanticEventFields, {name: str(row[name]) for name in SEMANTIC_FIELD_NAMES})


def backfill_current_generation(
    connection: pymysql.connections.Connection,
    *,
    schema: str,
    batch_size: int,
    expected_rows: int,
) -> dict[str, JsonValue]:
    """Backward-compatible wrapper that derives the requested current generation."""
    safe_schema = validated_stage_schema(schema)
    rows = _stage_rows(connection, schema=safe_schema)
    return backfill_generation(
        connection,
        schema=safe_schema,
        stage_generation_id=_snapshot_fingerprint(rows),
        batch_size=batch_size,
        expected_rows=expected_rows,
    )


def backfill_generation(
    connection: pymysql.connections.Connection,
    *,
    schema: str,
    stage_generation_id: str,
    batch_size: int,
    expected_rows: int | None = None,
) -> dict[str, JsonValue]:
    """Idempotently backfill one requested current-stage generation in bounded commits."""
    safe_schema = validated_stage_schema(schema)
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    rows = _stage_rows(connection, schema=safe_schema)
    if expected_rows is not None and len(rows) != expected_rows:
        raise RuntimeError(f"stage row count changed: expected={expected_rows}, actual={len(rows)}")
    generation_id = _snapshot_fingerprint(rows)
    if generation_id != stage_generation_id:
        raise RuntimeError(
            "requested stage generation does not match the current stage snapshot: "
            f"requested={stage_generation_id}, actual={generation_id}"
        )
    expected_count = len(rows)
    immediate_count, immediate_generation = _current_snapshot(connection, schema=safe_schema)
    if (immediate_count, immediate_generation) != (expected_count, generation_id):
        raise RuntimeError("stage snapshot changed between materialization and backfill")

    insert_sql = f"""
        INSERT INTO `{safe_schema}`.`{TARGET_TABLE}`
        (stage_generation_id, stage_row_id, semantic_event_key_v1, stage_row_sha256,
         source_file, source_sheet, source_row_no, source_file_sha256,
         backfill_batch_id, backfilled_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
    backfilled_at = datetime.now(timezone.utc).replace(tzinfo=None)
    batch_count = 0
    inserted_rows = 0
    reused_rows = 0
    for offset in range(0, len(rows), batch_size):
        batch = rows[offset : offset + batch_size]
        first_id = int(batch[0]["id"])
        last_id = int(batch[-1]["id"])
        batch_id = f"rtso-v1:{generation_id}:{first_id}-{last_id}"
        values = [
            (
                generation_id,
                int(row["id"]),
                semantic_event_key_v1(_semantic_fields(row)),
                str(row["stage_row_sha256"]),
                str(row["source_file"]),
                str(row["source_sheet"]),
                int(row["source_row_no"]),
                str(row["source_file_sha256"]),
                batch_id,
                backfilled_at,
            )
            for row in batch
        ]
        existing = _existing_batch_rows(
            connection,
            schema=safe_schema,
            stage_generation_id=generation_id,
            stage_row_ids=tuple(int(row["id"]) for row in batch),
        )
        missing_values = []
        for value in values:
            stage_row_id = int(value[1])
            previous = existing.get(stage_row_id)
            if previous is None:
                missing_values.append(value)
                continue
            _assert_existing_compatible(previous, value)
            reused_rows += 1
        if not missing_values:
            batch_count += 1
            continue
        with connection.cursor() as cursor:
            affected = cursor.executemany(insert_sql, missing_values)
        if affected != len(missing_values):
            connection.rollback()
            raise RuntimeError(
                f"bridge batch affected_rows mismatch: expected={len(missing_values)}, actual={affected}"
            )
        connection.commit()
        batch_count += 1
        inserted_rows += affected

    final_count, final_generation = _current_snapshot(connection, schema=safe_schema)
    if (final_count, final_generation) != (expected_count, generation_id):
        raise RuntimeError("stage snapshot changed during bounded bridge commits")
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT COUNT(*) AS row_count FROM `{safe_schema}`.`{TARGET_TABLE}` "
            "WHERE stage_generation_id=%s",
            (generation_id,),
        )
        bridge_count = int(cursor.fetchone()["row_count"])
    if bridge_count != expected_count:
        raise RuntimeError(
            f"bridge generation row count mismatch: expected={expected_count}, actual={bridge_count}"
        )
    return {
        "stage_generation_id": generation_id,
        "backfill_batch_prefix": f"rtso-v1:{generation_id}",
        "batch_size": batch_size,
        "batch_count": batch_count,
        "inserted_rows": inserted_rows,
        "reused_rows": reused_rows,
        "generation_rows": bridge_count,
        "backfilled_at_utc_naive": backfilled_at.isoformat(timespec="microseconds"),
    }


def _existing_batch_rows(
    connection: pymysql.connections.Connection,
    *,
    schema: str,
    stage_generation_id: str,
    stage_row_ids: tuple[int, ...],
) -> dict[int, dict[str, object]]:
    if not stage_row_ids:
        return {}
    placeholders = ",".join("%s" for _ in stage_row_ids)
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
                SELECT stage_row_id, semantic_event_key_v1, stage_row_sha256,
                       source_file, source_sheet, source_row_no, source_file_sha256,
                       backfill_batch_id
                FROM `{schema}`.`{TARGET_TABLE}`
                WHERE stage_generation_id=%s AND stage_row_id IN ({placeholders})
                ORDER BY stage_row_id
            """,
            (stage_generation_id, *stage_row_ids),
        )
        rows = cursor.fetchall()
    return {int(row["stage_row_id"]): cast(dict[str, object], row) for row in rows}


def _assert_existing_compatible(existing: dict[str, object], expected: tuple[object, ...]) -> None:
    comparable = (
        str(existing["semantic_event_key_v1"]),
        str(existing["stage_row_sha256"]),
        str(existing["source_file"]),
        str(existing["source_sheet"]),
        int(existing["source_row_no"]),
        str(existing["source_file_sha256"]),
        str(existing["backfill_batch_id"]),
    )
    expected_comparable = (
        str(expected[2]),
        str(expected[3]),
        str(expected[4]),
        str(expected[5]),
        int(expected[6]),
        str(expected[7]),
        str(expected[8]),
    )
    if comparable != expected_comparable:
        raise RuntimeError(
            f"existing bridge row differs for stage_row_id={int(expected[1])}"
        )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill the current Keyword stage occurrence bridge")
    parser.add_argument("--schema", default="jw_brand_activity_stage")
    parser.add_argument("--stage-generation-id", required=True)
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--expected-rows", type=int)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    connection = connect_mariadb(read_env_file())
    try:
        result = backfill_generation(
            connection,
            schema=args.schema,
            stage_generation_id=args.stage_generation_id,
            batch_size=args.batch_size,
            expected_rows=args.expected_rows,
        )
    finally:
        connection.close()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
