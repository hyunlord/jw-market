from __future__ import annotations

from typing import Any, Iterable

from .general_config import JSON_INSERT_COLUMNS, mariadb_connect
from .general_json import dumps

def ensure_json_columns(table: str, columns: Iterable[str]) -> None:
    """Add JSON columns required by newer mart writers when an existing DB is reused."""
    conn = mariadb_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SHOW COLUMNS FROM {table}")
            existing = {row["Field"] for row in cur.fetchall()}
            for column in columns:
                if column not in existing:
                    cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} JSON NULL")
    finally:
        conn.close()

def insert_rows(table: str, columns: list[str], rows: list[dict[str, Any]], batch_size: int = 500) -> None:
    if not rows:
        return
    placeholders = ",".join(["%s"] * len(columns))
    col_sql = ",".join(columns)
    update_cols = [col for col in columns if col not in {"brand_key", "atc4_code", "source", "measure"}]
    update_sql = ",".join([f"{col}=VALUES({col})" for col in update_cols])
    sql = f"INSERT INTO {table} ({col_sql}) VALUES ({placeholders}) ON DUPLICATE KEY UPDATE {update_sql}"
    payloads = []
    for row in rows:
        payloads.append(
            tuple(
                dumps(row.get(col)) if col in JSON_INSERT_COLUMNS else row.get(col)
                for col in columns
            )
        )
    conn = mariadb_connect()
    try:
        with conn.cursor() as cur:
            for start in range(0, len(payloads), batch_size):
                cur.executemany(sql, payloads[start : start + batch_size])
    finally:
        conn.close()

def delete_source_rows(table: str, source: str) -> None:
    conn = mariadb_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {table} WHERE source=%s", (source,))
    finally:
        conn.close()
