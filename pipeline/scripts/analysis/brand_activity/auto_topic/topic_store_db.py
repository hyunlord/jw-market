from __future__ import annotations

from dataclasses import dataclass
import json
import os
import time
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
STAGING_TOPICS_TABLE: Final = "mart_brand_activity_topics_staging"
STAGING_RUNS_TABLE: Final = "mart_brand_activity_topic_runs_staging"
TOPICS_TARGET_ENV: Final = "BRAND_ACTIVITY_TOPICS_TARGET_TABLE"
RUNS_TARGET_ENV: Final = "BRAND_ACTIVITY_TOPIC_RUNS_TARGET_TABLE"
COUNT_READBACK_RETRY_LIMIT: Final = 2
COUNT_READBACK_RETRY_DELAY_SECONDS: Final = 0.2


@dataclass(frozen=True, slots=True)
class StoreSummary:
    """Row-count evidence from one topic result upsert."""

    run_id: str
    topic_record_count: int
    topic_brand_count: int
    stored_topic_rows: int
    stored_run_rows: int
    count_retry_used: bool = False


@dataclass(frozen=True, slots=True)
class TopicTables:
    topics: str
    runs: str


APPROVED_TOPIC_TABLE_PAIRS: Final[frozenset[TopicTables]] = frozenset(
    {
        TopicTables(topics=TOPICS_TABLE, runs=RUNS_TABLE),
        TopicTables(topics=STAGING_TOPICS_TABLE, runs=STAGING_RUNS_TABLE),
    }
)


def resolve_topic_tables() -> TopicTables:
    """Resolve one approved live or staging table pair from the environment."""
    topics = os.environ.get(TOPICS_TARGET_ENV, TOPICS_TABLE).strip()
    runs = os.environ.get(RUNS_TARGET_ENV, RUNS_TABLE).strip()
    pair = TopicTables(topics=topics, runs=runs)
    if pair not in APPROVED_TOPIC_TABLE_PAIRS:
        raise TopicStoreError(
            "topic target tables must be the approved live or staging pair: "
            f"topics={topics!r}, runs={runs!r}"
        )
    return pair


def ensure_topic_tables(connection: pymysql.connections.Connection, *, schema: str = SCHEMA) -> None:
    """Create API-facing topic result tables inside the isolated schema."""
    safe_schema = validated_stage_schema(schema)
    tables = resolve_topic_tables()
    with connection.cursor() as cursor:
        for statement in topic_table_ddl(safe_schema, tables=tables):
            cursor.execute(statement)
        for statement in topic_table_migration_ddl(safe_schema, tables=tables):
            cursor.execute(statement)


def topic_table_ddl(
    schema: str = SCHEMA,
    *,
    tables: TopicTables | None = None,
) -> tuple[str, str]:
    """Return DDL for the topic payload and run metadata tables."""
    safe_schema = validated_stage_schema(schema)
    target = tables or resolve_topic_tables()
    return (_topics_ddl(safe_schema, target.topics), _runs_ddl(safe_schema, target.runs))


def topic_table_migration_ddl(
    schema: str = SCHEMA,
    *,
    tables: TopicTables | None = None,
) -> tuple[str, ...]:
    """Return idempotent schema evolution statements for existing topic marts."""
    safe_schema = validated_stage_schema(schema)
    target = tables or resolve_topic_tables()
    return (
        f"""
        ALTER TABLE `{safe_schema}`.`{target.runs}`
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
    tables = resolve_topic_tables()
    ensure_topic_tables(connection, schema=safe_schema)
    with connection.cursor() as cursor:
        cursor.execute(_run_upsert_sql(safe_schema, tables.runs), _run_tuple(run))
        cursor.executemany(
            _topic_upsert_sql(safe_schema, tables.topics),
            [_topic_tuple(record, run.run_id) for record in records],
        )
    connection.commit()
    with connection.cursor() as cursor:
        stored_run_rows, run_count_retry_used = _count_rows_with_bounded_retry(
            cursor,
            safe_schema,
            tables.runs,
            "run_id",
            run.run_id,
            expected_rows_present=True,
        )
        stored_topic_rows, topic_count_retry_used = _count_rows_with_bounded_retry(
            cursor,
            safe_schema,
            tables.topics,
            "run_id",
            run.run_id,
            expected_rows_present=bool(records),
        )
    return StoreSummary(
        run_id=run.run_id,
        topic_record_count=len(records),
        topic_brand_count=sum(record.brand_count for record in records),
        stored_topic_rows=stored_topic_rows,
        stored_run_rows=stored_run_rows,
        count_retry_used=run_count_retry_used or topic_count_retry_used,
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
        "count_retry_used": summary.count_retry_used,
    }


def _topics_ddl(schema: str, table: str = TOPICS_TABLE) -> str:
    """Return the topic payload table DDL."""
    return f"""
        CREATE TABLE IF NOT EXISTS `{schema}`.`{table}` (
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


def _runs_ddl(schema: str, table: str = RUNS_TABLE) -> str:
    """Return the topic run metadata table DDL."""
    return f"""
        CREATE TABLE IF NOT EXISTS `{schema}`.`{table}` (
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


def _run_upsert_sql(schema: str, table: str = RUNS_TABLE) -> str:
    """Return the run metadata upsert statement."""
    return f"""
        INSERT INTO `{schema}`.`{table}`
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


def _topic_upsert_sql(schema: str, table: str = TOPICS_TABLE) -> str:
    """Return the topic payload upsert statement."""
    return f"""
        INSERT INTO `{schema}`.`{table}`
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


def _count_rows_with_bounded_retry(
    cursor: pymysql.cursors.Cursor,
    schema: str,
    table: str,
    key: str,
    value: str,
    *,
    expected_rows_present: bool,
) -> tuple[int, bool]:
    """Recheck transient zero readbacks without assuming the unconfirmed root cause."""
    row_count = _count_rows(cursor, schema, table, key, value)
    if row_count > 0 or not expected_rows_present:
        return row_count, False

    # Observed false-negative readbacks exist, but the Galera/connection root cause is still unconfirmed.
    # Keep the retry short and bounded, and only trust a nonzero value that the DB actually returns.
    for _ in range(COUNT_READBACK_RETRY_LIMIT):
        time.sleep(COUNT_READBACK_RETRY_DELAY_SECONDS)
        retry_count = _count_rows(cursor, schema, table, key, value)
        if retry_count > 0:
            return retry_count, True
    return row_count, False
