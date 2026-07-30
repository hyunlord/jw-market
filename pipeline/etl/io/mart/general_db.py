from __future__ import annotations

import json
from pathlib import Path
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

def _insert_rows_with_cursor(
    cursor: Any,
    table: str,
    columns: list[str],
    rows: list[dict[str, Any]],
) -> None:
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
    cursor.executemany(sql, payloads)


def insert_rows(
    table: str,
    columns: list[str],
    rows: list[dict[str, Any]],
    batch_size: int = 500,
) -> None:
    if not rows:
        return
    conn = mariadb_connect()
    try:
        with conn.cursor() as cur:
            for start in range(0, len(rows), batch_size):
                _insert_rows_with_cursor(
                    cur,
                    table,
                    columns,
                    rows[start : start + batch_size],
                )
    finally:
        conn.close()

def delete_source_rows(table: str, source: str) -> None:
    conn = mariadb_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {table} WHERE source=%s", (source,))
    finally:
        conn.close()


def _iter_jsonl_batches(
    path: Path,
    *,
    batch_size: int,
) -> Iterable[list[dict[str, Any]]]:
    batch: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            batch.append(json.loads(line))
            if len(batch) >= batch_size:
                yield batch
                batch = []
    if batch:
        yield batch


def _delete_source_rows_in_batches(
    conn: Any,
    cursor: Any,
    table: str,
    source: str,
    *,
    batch_size: int,
    atc4_scope: tuple[str, ...] | None = None,
) -> None:
    predicate = "source=%s"
    params: tuple[object, ...] = (source,)
    if atc4_scope:
        placeholders = ",".join(["%s"] * len(atc4_scope))
        predicate += f" AND atc4_code IN ({placeholders})"
        params = (source, *atc4_scope)
    while True:
        deleted = int(
            cursor.execute(
                f"DELETE FROM {table} WHERE {predicate} ORDER BY id LIMIT %s",
                (*params, batch_size),
            )
            or 0
        )
        if deleted <= 0:
            return
        conn.commit()


def replace_source_rows_from_jsonl(
    *,
    source: str,
    brand_path: Path,
    market_path: Path,
    brand_columns: list[str],
    market_columns: list[str],
    batch_size: int = 500,
    commit_each_batch: bool = False,
) -> None:
    """Replace one source after all partition outputs are durable.

    Isolated build schemas may commit bounded batches because they are never
    published until the later atomic table-group rename succeeds.
    """
    conn = mariadb_connect()
    try:
        conn.autocommit(False)
        with conn.cursor() as cur:
            if commit_each_batch:
                for table in (
                    "mart_general_brand_metric",
                    "mart_general_market_metric",
                ):
                    _delete_source_rows_in_batches(
                        conn,
                        cur,
                        table,
                        source,
                        batch_size=batch_size,
                    )
            else:
                cur.execute(
                    "DELETE FROM mart_general_brand_metric WHERE source=%s",
                    (source,),
                )
                cur.execute(
                    "DELETE FROM mart_general_market_metric WHERE source=%s",
                    (source,),
                )
            for rows in _iter_jsonl_batches(brand_path, batch_size=batch_size):
                _insert_rows_with_cursor(
                    cur,
                    "mart_general_brand_metric",
                    brand_columns,
                    rows,
                )
                if commit_each_batch:
                    conn.commit()
            for rows in _iter_jsonl_batches(market_path, batch_size=batch_size):
                _insert_rows_with_cursor(
                    cur,
                    "mart_general_market_metric",
                    market_columns,
                    rows,
                )
                if commit_each_batch:
                    conn.commit()
        if not commit_each_batch:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def replace_scoped_source_rows_from_jsonl(
    *,
    source: str,
    atc4_scope: tuple[str, ...],
    brand_path: Path,
    market_path: Path,
    brand_columns: list[str],
    market_columns: list[str],
    batch_size: int = 100,
    commit_each_batch: bool = False,
) -> None:
    """Replace affected ATC4 rows in an unpublished clone using bounded writesets.

    With per-batch commits, a failed clone is safe to discard or retry: the
    retry deletes the same scope before replaying the deterministic JSONL.
    """

    scope = tuple(sorted({str(value).strip() for value in atc4_scope if str(value).strip()}))
    if not scope:
        raise ValueError("scoped source replacement requires at least one ATC4 code")
    placeholders = ",".join(["%s"] * len(scope))
    conn = mariadb_connect()
    try:
        conn.autocommit(False)
        with conn.cursor() as cur:
            for table in (
                "mart_general_brand_metric",
                "mart_general_market_metric",
            ):
                if commit_each_batch:
                    _delete_source_rows_in_batches(
                        conn,
                        cur,
                        table,
                        source,
                        batch_size=batch_size,
                        atc4_scope=scope,
                    )
                else:
                    cur.execute(
                        f"DELETE FROM {table} WHERE source=%s "
                        f"AND atc4_code IN ({placeholders})",
                        (source, *scope),
                    )
            for rows in _iter_jsonl_batches(brand_path, batch_size=batch_size):
                _insert_rows_with_cursor(
                    cur,
                    "mart_general_brand_metric",
                    brand_columns,
                    rows,
                )
                if commit_each_batch:
                    conn.commit()
            for rows in _iter_jsonl_batches(market_path, batch_size=batch_size):
                _insert_rows_with_cursor(
                    cur,
                    "mart_general_market_metric",
                    market_columns,
                    rows,
                )
                if commit_each_batch:
                    conn.commit()
        if not commit_each_batch:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
