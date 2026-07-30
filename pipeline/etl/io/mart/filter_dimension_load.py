from __future__ import annotations

"""DDL and Galera-safe writes for the dynamic filter dimension sidecar."""

from collections.abc import Sequence
from typing import Any

import pymysql

from pipeline.contracts.dimension_registry import dimension_value_hash
from .filter_dimension_metric import FILTER_DIMENSION_TABLE
from .filter_dimension_metric import guard_dimension_stage_target
from .general_json import dumps


def quote_id(identifier: str) -> str:
    return "`" + identifier.replace("`", "``") + "`"


def filter_dimension_table_ddl(qualified_table: str | None = None) -> str:
    quoted = qualified_table or quote_id(FILTER_DIMENSION_TABLE)
    return f"""
CREATE TABLE IF NOT EXISTS {quoted} (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  source VARCHAR(16) NOT NULL,
  measure VARCHAR(32) NOT NULL,
  atc4_code VARCHAR(16) NOT NULL,
  brand_key VARCHAR(255) NOT NULL,
  brand_name VARCHAR(255) NOT NULL,
  product_code VARCHAR(255) NOT NULL,
  dimension_type VARCHAR(64) NOT NULL,
  dimension_value TEXT NOT NULL,
  dimension_value_norm TEXT NOT NULL,
  dimension_value_hash CHAR(64) NOT NULL,
  raw_value_history JSON NOT NULL,
  computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_filter_dimension (
    source, measure, atc4_code, brand_key, product_code, dimension_type, dimension_value_hash
  ),
  INDEX idx_filter_lookup (source, measure, dimension_type, dimension_value_hash, atc4_code, brand_key),
  INDEX idx_filter_atc_brand (source, measure, atc4_code, brand_key),
  INDEX idx_filter_option (source, dimension_type, dimension_value_hash),
  INDEX idx_filter_norm_prefix (source, dimension_type, dimension_value_norm(191)),
  INDEX idx_general_option_universe (source, dimension_type, dimension_value_hash, dimension_value_norm(191)),
  INDEX idx_general_atc_scope (source, atc4_code, dimension_type, dimension_value_hash),
  INDEX idx_general_brand_scope (source, atc4_code, brand_key, dimension_type, dimension_value_hash)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
""".strip()


def create_filter_dimension_table(
    conn: pymysql.connections.Connection,
    target_db: str,
    *,
    allow_local_serving_target: bool = False,
) -> None:
    guard_dimension_stage_target(target_db, allow_local_serving_target=allow_local_serving_target)
    qualified = f"{quote_id(target_db)}.{quote_id(FILTER_DIMENSION_TABLE)}"
    with conn.cursor() as cur:
        cur.execute(f"CREATE DATABASE IF NOT EXISTS {quote_id(target_db)} DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci")
        cur.execute(f"DROP TABLE IF EXISTS {qualified}")
        cur.execute(filter_dimension_table_ddl(qualified))


def insert_filter_dimension_rows(
    conn: pymysql.connections.Connection,
    target_db: str,
    rows: Sequence[dict[str, Any]],
    *,
    batch_size: int = 200,
    allow_local_serving_target: bool = False,
) -> None:
    guard_dimension_stage_target(target_db, allow_local_serving_target=allow_local_serving_target)
    if batch_size > 200:
        raise ValueError("batch_size must be <= 200 for Galera writeset safety")
    if not rows:
        return
    columns = [
        "source",
        "measure",
        "atc4_code",
        "brand_key",
        "brand_name",
        "product_code",
        "dimension_type",
        "dimension_value",
        "dimension_value_norm",
        "dimension_value_hash",
        "raw_value_history",
    ]
    sql = (
        f"INSERT INTO {quote_id(target_db)}.{quote_id(FILTER_DIMENSION_TABLE)} "
        f"({','.join(quote_id(col) for col in columns)}) VALUES ({','.join(['%s'] * len(columns))})"
    )
    payloads = [
        tuple(_column_value(row, col) for col in columns)
        for row in rows
    ]
    with conn.cursor() as cur:
        for start in range(0, len(payloads), batch_size):
            cur.executemany(sql, payloads[start : start + batch_size])


def copy_filter_dimension_source_rows(
    conn: pymysql.connections.Connection,
    source_db: str,
    target_db: str,
    source: str,
    *,
    batch_size: int = 200,
    allow_local_serving_target: bool = False,
) -> dict[str, Any]:
    """Copy verified sidecar rows between isolated schemas in Galera-safe chunks.

    STAGE B reuses the already-verified UBIST STAGE A sidecar while building new
    IQVIA rows. Keeping this copy path tracked here prevents ad hoc INSERT...SELECT
    use, preserves the live-schema guard, and records copy completeness in the
    build manifest.
    """
    guard_dimension_stage_target(source_db)
    guard_dimension_stage_target(target_db, allow_local_serving_target=allow_local_serving_target)
    if source_db == target_db:
        raise ValueError("copy source and target schemas must differ")
    if batch_size > 200:
        raise ValueError("batch_size must be <= 200 for Galera writeset safety")

    columns = [
        "source",
        "measure",
        "atc4_code",
        "brand_key",
        "brand_name",
        "product_code",
        "dimension_type",
        "dimension_value",
        "dimension_value_norm",
        "dimension_value_hash",
        "raw_value_history",
    ]
    select_columns = ", ".join(quote_id(column) for column in columns)
    copied = 0
    last_id = 0
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT COUNT(*) AS n FROM {quote_id(source_db)}.{quote_id(FILTER_DIMENSION_TABLE)} WHERE source=%s",
            (source,),
        )
        expected = int(cur.fetchone()["n"])
        while True:
            cur.execute(
                f"""
                INSERT INTO {quote_id(target_db)}.{quote_id(FILTER_DIMENSION_TABLE)}
                    ({select_columns})
                SELECT {select_columns}
                FROM {quote_id(source_db)}.{quote_id(FILTER_DIMENSION_TABLE)}
                WHERE source=%s AND id > %s
                ORDER BY id
                LIMIT {int(batch_size)}
                """,
                (source, last_id),
            )
            inserted = int(cur.rowcount)
            if inserted == 0:
                break
            copied += inserted
            cur.execute(
                f"""
                SELECT MAX(id) AS max_id
                FROM (
                    SELECT id
                    FROM {quote_id(source_db)}.{quote_id(FILTER_DIMENSION_TABLE)}
                    WHERE source=%s AND id > %s
                    ORDER BY id
                    LIMIT {int(inserted)}
                ) AS copied_rows
                """,
                (source, last_id),
            )
            last_id = int(cur.fetchone()["max_id"])
    return {
        "copy_from": source_db,
        "copied_rows": copied,
        "expected_rows": expected,
        "copy_complete": copied == expected,
    }


def _column_value(row: dict[str, Any], column: str) -> Any:
    if column == "raw_value_history":
        return dumps(row[column])
    if column == "dimension_value_hash":
        return dimension_value_hash(str(row["dimension_value_norm"]))
    return row[column]
