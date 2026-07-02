from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Final

import pymysql

from .data_source import SCHEMA
from .models import JsonValue
from .topic_store import (
    RunRecord,
    TopicArtifacts,
    TopicRecord,
    TopicStoreError,
    build_run_record,
    build_topic_records,
    validated_stage_schema,
)


TOPICS_TABLE: Final = "mart_brand_activity_topics"
RUNS_TABLE: Final = "mart_brand_activity_topic_runs"


@dataclass(frozen=True, slots=True)
class StoreSummary:
    """Row-count evidence from one topic result upsert."""

    run_id: str
    topic_record_count: int
    topic_brand_count: int
    stored_topic_rows: int
    stored_run_rows: int


def ensure_topic_tables(connection: pymysql.connections.Connection, *, schema: str = SCHEMA) -> None:
    """Create API-facing topic result tables inside the isolated schema."""
    safe_schema = validated_stage_schema(schema)
    with connection.cursor() as cursor:
        for statement in topic_table_ddl(safe_schema):
            cursor.execute(statement)
        for statement in topic_table_migration_ddl(safe_schema):
            cursor.execute(statement)


def topic_table_ddl(schema: str = SCHEMA) -> tuple[str, str]:
    """Return DDL for the topic payload and run metadata tables."""
    safe_schema = validated_stage_schema(schema)
    return (_topics_ddl(safe_schema), _runs_ddl(safe_schema))


def topic_table_migration_ddl(schema: str = SCHEMA) -> tuple[str, ...]:
    """Return idempotent schema evolution statements for existing topic marts."""
    safe_schema = validated_stage_schema(schema)
    return (
        f"""
        ALTER TABLE `{safe_schema}`.`{RUNS_TABLE}`
        ADD COLUMN IF NOT EXISTS input_fingerprint CHAR(64) NULL AFTER sha256
        """,
    )


def upsert_topic_results(
    connection: pymysql.connections.Connection,
    *,
    schema: str,
    run: RunRecord,
    records: list[TopicRecord],
) -> StoreSummary:
    """Upsert one measured run and its API-ready market payloads."""
    safe_schema = validated_stage_schema(schema)
    ensure_topic_tables(connection, schema=safe_schema)
    with connection.cursor() as cursor:
        cursor.execute(_run_upsert_sql(safe_schema), _run_tuple(run))
        cursor.executemany(_topic_upsert_sql(safe_schema), [_topic_tuple(record, run.run_id) for record in records])
    connection.commit()
    with connection.cursor() as cursor:
        stored_run_rows = _count_rows(cursor, safe_schema, RUNS_TABLE, "run_id", run.run_id)
        stored_topic_rows = _count_rows(cursor, safe_schema, TOPICS_TABLE, "run_id", run.run_id)
    return StoreSummary(
        run_id=run.run_id,
        topic_record_count=len(records),
        topic_brand_count=sum(record.brand_count for record in records),
        stored_topic_rows=stored_topic_rows,
        stored_run_rows=stored_run_rows,
    )


def ensure_store_summary_nonzero(summary: StoreSummary) -> None:
    """Raise when a measured save produced records but no persisted row evidence."""
    if summary.topic_record_count > 0 and (summary.stored_run_rows < 1 or summary.stored_topic_rows < 1):
        raise TopicStoreError(
            "zero-row DB save for "
            f"{summary.run_id}: built topics={summary.topic_record_count}, "
            f"built brands={summary.topic_brand_count}, "
            f"stored_run_rows={summary.stored_run_rows}, "
            f"stored_topic_rows={summary.stored_topic_rows}"
        )


def save_artifacts(
    connection: pymysql.connections.Connection,
    *,
    schema: str,
    artifacts: TopicArtifacts,
    artifact_sha256: str,
) -> StoreSummary:
    """Build records from artifacts and upsert them into the isolated stage schema."""
    return upsert_topic_results(
        connection,
        schema=schema,
        run=build_run_record(artifacts, artifact_sha256=artifact_sha256),
        records=build_topic_records(artifacts),
    )


def store_summary_json(summary: StoreSummary) -> dict[str, JsonValue]:
    """Serialize one store summary for audit and CLI output."""
    return {
        "run_id": summary.run_id,
        "topic_record_count": summary.topic_record_count,
        "topic_brand_count": summary.topic_brand_count,
        "stored_topic_rows": summary.stored_topic_rows,
        "stored_run_rows": summary.stored_run_rows,
    }


def _topics_ddl(schema: str) -> str:
    """Return the topic payload table DDL."""
    return f"""
        CREATE TABLE IF NOT EXISTS `{schema}`.`{TOPICS_TABLE}` (
            scope_id VARCHAR(128) NOT NULL PRIMARY KEY,
            display_name VARCHAR(255) NOT NULL,
            atc4_values JSON NOT NULL,
            quality_grade VARCHAR(8) NOT NULL,
            source_row_count INT NOT NULL,
            payload JSON NOT NULL,
            run_id VARCHAR(160) NOT NULL,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            KEY idx_mart_brand_activity_topics_run_id (run_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """


def _runs_ddl(schema: str) -> str:
    """Return the topic run metadata table DDL."""
    return f"""
        CREATE TABLE IF NOT EXISTS `{schema}`.`{RUNS_TABLE}` (
            run_id VARCHAR(160) NOT NULL PRIMARY KEY,
            created_at DATETIME NOT NULL,
            model_id VARCHAR(128) NOT NULL,
            serving_id VARCHAR(32) NOT NULL,
            route VARCHAR(64) NOT NULL,
            total_prompt_tokens BIGINT NOT NULL,
            total_completion_tokens BIGINT NOT NULL,
            est_cost_usd DECIMAL(12,4) NOT NULL,
            market_count INT NOT NULL,
            brand_count INT NOT NULL,
            axis_compound_count INT NOT NULL,
            brand_specific_dup_count INT NOT NULL,
            sha256 CHAR(64) NOT NULL,
            input_fingerprint CHAR(64) NULL,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """


def _run_upsert_sql(schema: str) -> str:
    """Return the run metadata upsert statement."""
    return f"""
        INSERT INTO `{schema}`.`{RUNS_TABLE}`
        (run_id, created_at, model_id, serving_id, route, total_prompt_tokens, total_completion_tokens,
         est_cost_usd, market_count, brand_count, axis_compound_count, brand_specific_dup_count, sha256,
         input_fingerprint)
        VALUES ({", ".join(["%s"] * 14)})
        ON DUPLICATE KEY UPDATE
            created_at=VALUES(created_at),
            model_id=VALUES(model_id),
            serving_id=VALUES(serving_id),
            route=VALUES(route),
            total_prompt_tokens=VALUES(total_prompt_tokens),
            total_completion_tokens=VALUES(total_completion_tokens),
            est_cost_usd=VALUES(est_cost_usd),
            market_count=VALUES(market_count),
            brand_count=VALUES(brand_count),
            axis_compound_count=VALUES(axis_compound_count),
            brand_specific_dup_count=VALUES(brand_specific_dup_count),
            sha256=VALUES(sha256),
            input_fingerprint=VALUES(input_fingerprint)
    """


def _topic_upsert_sql(schema: str) -> str:
    """Return the topic payload upsert statement."""
    return f"""
        INSERT INTO `{schema}`.`{TOPICS_TABLE}`
        (scope_id, display_name, atc4_values, quality_grade, source_row_count, payload, run_id)
        VALUES ({", ".join(["%s"] * 7)})
        ON DUPLICATE KEY UPDATE
            display_name=VALUES(display_name),
            atc4_values=VALUES(atc4_values),
            quality_grade=VALUES(quality_grade),
            source_row_count=VALUES(source_row_count),
            payload=VALUES(payload),
            run_id=VALUES(run_id)
    """


def _run_tuple(run: RunRecord) -> tuple[JsonValue, ...]:
    """Return one DB tuple for run metadata."""
    return (
        run.run_id,
        run.created_at,
        run.model_id,
        run.serving_id,
        run.route,
        run.total_prompt_tokens,
        run.total_completion_tokens,
        run.est_cost_usd,
        run.market_count,
        run.brand_count,
        run.axis_compound_count,
        run.brand_specific_dup_count,
        run.sha256,
        run.input_fingerprint,
    )


def _topic_tuple(record: TopicRecord, run_id: str) -> tuple[JsonValue, ...]:
    """Return one DB tuple for a market topic payload."""
    return (
        record.scope_id,
        record.display_name,
        json.dumps(list(record.atc4_values), ensure_ascii=False, sort_keys=True),
        record.quality_grade,
        record.source_row_count,
        json.dumps(record.payload, ensure_ascii=False, sort_keys=True),
        run_id,
    )


def _count_rows(cursor: pymysql.cursors.Cursor, schema: str, table: str, key: str, value: str) -> int:
    """Count rows for idempotency evidence after an upsert."""
    cursor.execute(f"SELECT COUNT(*) AS row_count FROM `{schema}`.`{table}` WHERE `{key}` = %s", (value,))
    row = cursor.fetchone()
    if isinstance(row, dict):
        value = row.get("row_count")
        return int(value) if isinstance(value, int | float) else 0
    return int(row[0])
