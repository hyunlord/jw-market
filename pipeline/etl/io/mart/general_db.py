from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable

from .general_config import JSON_INSERT_COLUMNS, mariadb_connect
from .general_json import dumps
from .general_period_merge import merge_scoped_row


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


def _iter_jsonl_atc4_groups(path: Path) -> Iterable[tuple[str, list[dict[str, Any]]]]:
    current: str | None = None
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for batch in _iter_jsonl_batches(path, batch_size=100):
        for row in batch:
            atc4_code = str(row.get("atc4_code") or "").strip()
            if not atc4_code:
                raise ValueError(f"scoped candidate row has no atc4_code: {path}")
            if current is None:
                current = atc4_code
            if atc4_code != current:
                if atc4_code in seen:
                    raise ValueError(
                        f"scoped candidate rows are not contiguous for {atc4_code}: {path}"
                    )
                seen.add(current)
                yield current, rows
                current = atc4_code
                rows = []
            rows.append(row)
    if current is not None:
        yield current, rows


def _decode_json_columns(row: dict[str, Any]) -> dict[str, Any]:
    decoded = dict(row)
    for column in JSON_INSERT_COLUMNS:
        value = decoded.get(column)
        if isinstance(value, str):
            decoded[column] = json.loads(value)
    return decoded


def _scoped_row_key(table: str, row: dict[str, Any]) -> tuple[str, ...]:
    if table == "mart_general_brand_metric":
        return tuple(
            str(row.get(column) or "")
            for column in ("brand_key", "atc4_code", "source", "measure")
        )
    return tuple(
        str(row.get(column) or "")
        for column in ("atc4_code", "source", "measure")
    )


def _index_scoped_rows(
    table: str,
    rows: Iterable[dict[str, Any]],
) -> dict[tuple[str, ...], dict[str, Any]]:
    indexed: dict[tuple[str, ...], dict[str, Any]] = {}
    for row in rows:
        key = _scoped_row_key(table, row)
        if key in indexed:
            raise ValueError(f"duplicate scoped row key table={table} key={key}")
        indexed[key] = row
    return indexed


def _retain_merged_row(table: str, row: dict[str, Any]) -> bool:
    if table == "mart_general_brand_metric":
        if "metric_history" not in row and "raw_value_history" not in row:
            return True
        return bool(row.get("metric_history") or row.get("raw_value_history"))
    if "market_size_series" not in row:
        return True
    return bool(row.get("market_size_series"))


def _fetch_scoped_rows(
    cursor: Any,
    *,
    table: str,
    columns: list[str],
    source: str,
    atc4_code: str,
) -> list[dict[str, Any]]:
    cursor.execute(
        f"SELECT {','.join(columns)} FROM {table} "
        "WHERE source=%s AND atc4_code=%s ORDER BY id",
        (source, atc4_code),
    )
    return [_decode_json_columns(row) for row in cursor.fetchall()]


def _prepare_period_merged_jsonl(
    cursor: Any,
    *,
    table: str,
    columns: list[str],
    source: str,
    scope: tuple[str, ...],
    period_scope: tuple[str, ...],
    source_periods: tuple[str, ...] | None,
    candidate_path: Path,
) -> Path:
    scope_set = set(scope)
    seen: set[str] = set()
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix=f"{table}-period-merge-",
        suffix=".jsonl",
        dir=candidate_path.parent,
        delete=False,
    )
    output_path = Path(handle.name)
    try:
        groups = _iter_jsonl_atc4_groups(candidate_path)
        for atc4_code, candidate_rows in groups:
            if atc4_code not in scope_set:
                raise ValueError(
                    f"candidate ATC4 {atc4_code} is outside requested scope"
                )
            seen.add(atc4_code)
            existing_rows = _fetch_scoped_rows(
                cursor,
                table=table,
                columns=columns,
                source=source,
                atc4_code=atc4_code,
            )
            old_index = _index_scoped_rows(table, existing_rows)
            new_index = _index_scoped_rows(table, candidate_rows)
            for key in sorted(old_index.keys() | new_index.keys()):
                merged = merge_scoped_row(
                    old_index.get(key),
                    new_index.get(key),
                    period_scope=period_scope,
                    source_periods=source_periods,
                )
                if _retain_merged_row(table, merged):
                    handle.write(dumps(merged) + "\n")
        for atc4_code in sorted(scope_set - seen):
            existing_rows = _fetch_scoped_rows(
                cursor,
                table=table,
                columns=columns,
                source=source,
                atc4_code=atc4_code,
            )
            for existing in existing_rows:
                merged = merge_scoped_row(
                    existing,
                    None,
                    period_scope=period_scope,
                    source_periods=source_periods,
                )
                if _retain_merged_row(table, merged):
                    handle.write(dumps(merged) + "\n")
    except Exception:
        handle.close()
        output_path.unlink(missing_ok=True)
        raise
    handle.close()
    return output_path


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
    period_scope: tuple[str, ...],
    source_periods: tuple[str, ...] | None = None,
    brand_path: Path,
    market_path: Path,
    brand_columns: list[str],
    market_columns: list[str],
    batch_size: int = 100,
    commit_each_batch: bool = False,
) -> None:
    """Replace requested periods in an unpublished clone using bounded writesets.

    Candidate rows are merged with the cloned baseline before any mutation.
    With per-batch commits, a failed clone is safe to discard or retry because
    no partially written build schema can be published.
    """

    scope = tuple(sorted({str(value).strip() for value in atc4_scope if str(value).strip()}))
    if not scope:
        raise ValueError("scoped source replacement requires at least one ATC4 code")
    periods = tuple(
        sorted({str(value).strip() for value in period_scope if str(value).strip()})
    )
    if not periods:
        raise ValueError("scoped source replacement requires a period scope")
    placeholders = ",".join(["%s"] * len(scope))
    conn = mariadb_connect()
    merged_paths: list[Path] = []
    try:
        conn.autocommit(False)
        with conn.cursor() as cur:
            merged_brand_path = _prepare_period_merged_jsonl(
                cur,
                table="mart_general_brand_metric",
                columns=brand_columns,
                source=source,
                scope=scope,
                period_scope=periods,
                source_periods=source_periods,
                candidate_path=brand_path,
            )
            merged_paths.append(merged_brand_path)
            merged_market_path = _prepare_period_merged_jsonl(
                cur,
                table="mart_general_market_metric",
                columns=market_columns,
                source=source,
                scope=scope,
                period_scope=periods,
                source_periods=source_periods,
                candidate_path=market_path,
            )
            merged_paths.append(merged_market_path)
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
            for rows in _iter_jsonl_batches(merged_brand_path, batch_size=batch_size):
                _insert_rows_with_cursor(
                    cur,
                    "mart_general_brand_metric",
                    brand_columns,
                    rows,
                )
                if commit_each_batch:
                    conn.commit()
            for rows in _iter_jsonl_batches(merged_market_path, batch_size=batch_size):
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
        for path in merged_paths:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        conn.close()
