from __future__ import annotations

"""DDL and Galera-safe writes for the dynamic filter dimension sidecar."""

from collections.abc import Sequence
import hashlib
from typing import Any

import pymysql

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
  INDEX idx_filter_norm_prefix (source, dimension_type, dimension_value_norm(191))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
""".strip()


def create_filter_dimension_table(conn: pymysql.connections.Connection, target_db: str) -> None:
    guard_dimension_stage_target(target_db)
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
) -> None:
    guard_dimension_stage_target(target_db)
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


def _column_value(row: dict[str, Any], column: str) -> Any:
    if column == "raw_value_history":
        return dumps(row[column])
    if column == "dimension_value_hash":
        return hashlib.sha256(str(row["dimension_value_norm"]).encode("utf-8")).hexdigest()
    return row[column]
