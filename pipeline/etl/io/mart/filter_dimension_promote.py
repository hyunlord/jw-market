from __future__ import annotations

from collections.abc import Sequence
import hashlib
import re
import time
from typing import Any

import pymysql

from pipeline.etl.io.mart.filter_dimension_load import quote_id
from pipeline.etl.io.mart.general_json import dumps
from pipeline.etl.io.mart.filter_dimension_metric import FILTER_DIMENSION_TABLE
from pipeline.etl.io.mart.filter_dimension_metric import guard_dimension_stage_target


_PROMOTION_COLUMNS = (
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
)
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_]+$")


def promote_filter_dimension_rows(
    conn: pymysql.connections.Connection,
    rows: Sequence[dict[str, Any]],
    target_db: str,
    *,
    source: str,
    dimension_type: str,
    build_marker: str,
    batch_size: int = 200,
    allow_shared_serving_target: bool = False,
    promotion_run_id: str,
) -> dict[str, Any]:
    """Promote a fully computed in-memory slice when staging DDL is unavailable."""

    _validate_promotion_target(
        target_db,
        source=source,
        dimension_type=dimension_type,
        batch_size=batch_size,
        allow_shared_serving_target=allow_shared_serving_target,
    )
    if not rows:
        raise RuntimeError("refusing to replace ubist/molecule with an empty computed slice")
    if any(row.get("source") != source or row.get("dimension_type") != dimension_type for row in rows):
        raise ValueError("computed rows contain a mixed or out-of-scope sidecar slice")

    payloads = [_promotion_payload(row, build_marker) for row in rows]
    unique_keys = {
        (
            payload[0],
            payload[1],
            payload[2],
            payload[3],
            payload[5],
            payload[6],
            payload[9],
        )
        for payload in payloads
    }
    if len(unique_keys) != len(payloads):
        raise ValueError("computed ubist/molecule slice contains duplicate serving keys")

    backup = create_filter_dimension_backup(conn, target_db, promotion_run_id)
    target_table = f"{quote_id(target_db)}.{quote_id(FILTER_DIMENSION_TABLE)}"
    promoted = _upsert_payloads(
        conn,
        target_table=target_table,
        payloads=payloads,
        batch_size=batch_size,
    )
    expected = len(payloads)
    _require_complete_promotion(
        conn,
        target_table=target_table,
        source=source,
        dimension_type=dimension_type,
        build_marker=build_marker,
        expected=expected,
        promoted=promoted,
    )
    stale_deleted = _delete_stale_slice(
        conn,
        target_table=target_table,
        source=source,
        dimension_type=dimension_type,
        build_marker=build_marker,
        batch_size=batch_size,
    )
    return {
        "source_db": None,
        "target_db": target_db,
        "source": source,
        "dimension_type": dimension_type,
        "build_marker": build_marker,
        "expected_rows": expected,
        "promoted_rows": promoted,
        "stale_rows_deleted": stale_deleted,
        "mode": "computed_rows_direct",
        "backup": backup,
    }


def promote_filter_dimension_slice(
    conn: pymysql.connections.Connection,
    source_db: str,
    target_db: str,
    *,
    source: str,
    dimension_type: str,
    build_marker: str,
    batch_size: int = 200,
    allow_shared_serving_target: bool = False,
    promotion_run_id: str,
) -> dict[str, Any]:
    """Promote one verified sidecar slice without touching adjacent dimensions."""

    guard_dimension_stage_target(source_db)
    _validate_promotion_target(
        target_db,
        source=source,
        dimension_type=dimension_type,
        batch_size=batch_size,
        allow_shared_serving_target=allow_shared_serving_target,
    )

    source_table = f"{quote_id(source_db)}.{quote_id(FILTER_DIMENSION_TABLE)}"
    target_table = f"{quote_id(target_db)}.{quote_id(FILTER_DIMENSION_TABLE)}"
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT COUNT(*) AS n FROM {source_table} WHERE source=%s AND dimension_type=%s",
            (source, dimension_type),
        )
        expected = int(cur.fetchone()["n"])
    if expected < 1:
        raise RuntimeError("refusing to replace ubist/molecule with an empty staged slice")

    backup = create_filter_dimension_backup(conn, target_db, promotion_run_id)
    promoted = _upsert_slice(
        conn,
        source_table=source_table,
        target_table=target_table,
        source=source,
        dimension_type=dimension_type,
        build_marker=build_marker,
        batch_size=batch_size,
    )
    _require_complete_promotion(
        conn,
        target_table=target_table,
        source=source,
        dimension_type=dimension_type,
        build_marker=build_marker,
        expected=expected,
        promoted=promoted,
    )
    stale_deleted = _delete_stale_slice(
        conn,
        target_table=target_table,
        source=source,
        dimension_type=dimension_type,
        build_marker=build_marker,
        batch_size=batch_size,
    )
    return {
        "source_db": source_db,
        "target_db": target_db,
        "source": source,
        "dimension_type": dimension_type,
        "build_marker": build_marker,
        "expected_rows": expected,
        "promoted_rows": promoted,
        "stale_rows_deleted": stale_deleted,
        "backup": backup,
    }


def create_filter_dimension_backup(
    conn: pymysql.connections.Connection,
    target_db: str,
    promotion_run_id: str,
) -> dict[str, Any]:
    if not _RUN_ID_RE.fullmatch(promotion_run_id):
        raise ValueError("promotion_run_id must contain only letters, numbers, and underscores")
    backup_table = f"{FILTER_DIMENSION_TABLE}__old_{promotion_run_id}"
    if len(backup_table) > 64:
        raise ValueError(f"FDM backup identifier exceeds 64 characters: {backup_table}")
    if _table_exists(conn, target_db, backup_table):
        raise RuntimeError(f"FDM backup already exists: {target_db}.{backup_table}")
    live = f"{quote_id(target_db)}.{quote_id(FILTER_DIMENSION_TABLE)}"
    backup = f"{quote_id(target_db)}.{quote_id(backup_table)}"
    live_rows = _table_count(conn, live)
    if live_rows < 1:
        raise RuntimeError(f"refusing to back up empty FDM table: {target_db}.{FILTER_DIMENSION_TABLE}")
    with conn.cursor() as cur:
        cur.execute(f"CREATE TABLE {backup} LIKE {live}")
        cur.execute(f"INSERT INTO {backup} SELECT * FROM {live}")
    conn.commit()
    backup_rows = _table_count(conn, backup)
    if backup_rows != live_rows:
        raise RuntimeError(f"FDM backup row count mismatch: {backup_rows} != {live_rows}")
    return {"table": backup_table, "row_count": backup_rows, "promotion_run_id": promotion_run_id}


def _table_exists(conn: pymysql.connections.Connection, db_name: str, table_name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS table_count FROM information_schema.tables "
            "WHERE table_schema=%s AND table_name=%s",
            (db_name, table_name),
        )
        return int(cur.fetchone()["table_count"]) > 0


def _table_count(conn: pymysql.connections.Connection, qualified_table: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS n FROM {qualified_table}")
        return int(cur.fetchone()["n"])


def _upsert_slice(
    conn: pymysql.connections.Connection,
    *,
    source_table: str,
    target_table: str,
    source: str,
    dimension_type: str,
    build_marker: str,
    batch_size: int,
) -> int:
    promoted = 0
    last_id = 0
    select_columns = ", ".join(quote_id(column) for column in _PROMOTION_COLUMNS)
    placeholders = ",".join(["%s"] * len(_PROMOTION_COLUMNS))
    upsert = (
        f"INSERT INTO {target_table} ({select_columns}, computed_at) VALUES ({placeholders}, %s) "
        "ON DUPLICATE KEY UPDATE "
        "brand_name=VALUES(brand_name), dimension_value=VALUES(dimension_value), "
        "dimension_value_norm=VALUES(dimension_value_norm), raw_value_history=VALUES(raw_value_history), "
        "computed_at=VALUES(computed_at)"
    )
    while True:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, {select_columns}
                FROM {source_table}
                WHERE source=%s AND dimension_type=%s AND id>%s
                ORDER BY id
                LIMIT {int(batch_size)}
                """,
                (source, dimension_type, last_id),
            )
            rows = list(cur.fetchall())
        if not rows:
            return promoted
        payloads = [
            tuple(row[column] for column in _PROMOTION_COLUMNS) + (build_marker,)
            for row in rows
        ]
        with conn.cursor() as cur:
            cur.executemany(upsert, payloads)
        conn.commit()
        promoted += len(rows)
        last_id = int(rows[-1]["id"])


def _validate_promotion_target(
    target_db: str,
    *,
    source: str,
    dimension_type: str,
    batch_size: int,
    allow_shared_serving_target: bool,
) -> None:
    if not allow_shared_serving_target:
        raise ValueError("shared serving target promotion requires explicit approval")
    if source != "ubist" or dimension_type != "molecule":
        raise ValueError("this promotion path is restricted to the ubist/molecule slice")
    if batch_size < 1 or batch_size > 200:
        raise ValueError("batch_size must be between 1 and 200")
    if "`" in target_db or not target_db.replace("_", "").isalnum():
        raise ValueError(f"unsafe target schema name: {target_db}")


def _promotion_payload(row: dict[str, Any], build_marker: str) -> tuple[Any, ...]:
    normalized = str(row["dimension_value_norm"])
    values = {
        **row,
        "dimension_value_hash": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        "raw_value_history": dumps(row["raw_value_history"]),
    }
    return tuple(values[column] for column in _PROMOTION_COLUMNS) + (build_marker,)


def _upsert_payloads(
    conn: pymysql.connections.Connection,
    *,
    target_table: str,
    payloads: Sequence[tuple[Any, ...]],
    batch_size: int,
) -> int:
    select_columns = ", ".join(quote_id(column) for column in _PROMOTION_COLUMNS)
    placeholders = ",".join(["%s"] * len(_PROMOTION_COLUMNS))
    upsert = (
        f"INSERT INTO {target_table} ({select_columns}, computed_at) VALUES ({placeholders}, %s) "
        "ON DUPLICATE KEY UPDATE "
        "brand_name=VALUES(brand_name), dimension_value=VALUES(dimension_value), "
        "dimension_value_norm=VALUES(dimension_value_norm), raw_value_history=VALUES(raw_value_history), "
        "computed_at=VALUES(computed_at)"
    )
    promoted = 0
    for start in range(0, len(payloads), batch_size):
        batch = payloads[start : start + batch_size]
        with conn.cursor() as cur:
            cur.executemany(upsert, batch)
        conn.commit()
        promoted += len(batch)
    return promoted


def _require_complete_promotion(
    conn: pymysql.connections.Connection,
    *,
    target_table: str,
    source: str,
    dimension_type: str,
    build_marker: str,
    expected: int,
    promoted: int,
) -> None:
    marked = 0
    if promoted == expected:
        for attempt in range(30):
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT COUNT(*) AS n FROM {target_table} "
                    "WHERE source=%s AND dimension_type=%s AND computed_at=%s",
                    (source, dimension_type, build_marker),
                )
                marked = int(cur.fetchone()["n"])
            if marked == expected:
                return
            if attempt < 29:
                time.sleep(1)
    if promoted != expected or marked != expected:
        raise RuntimeError(
            f"sidecar promotion incomplete: promoted={promoted} marked={marked} expected={expected}"
        )


def _delete_stale_slice(
    conn: pymysql.connections.Connection,
    *,
    target_table: str,
    source: str,
    dimension_type: str,
    build_marker: str,
    batch_size: int,
) -> int:
    stale_deleted = 0
    while True:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                DELETE FROM {target_table}
                WHERE source=%s AND dimension_type=%s AND computed_at<>%s
                LIMIT {int(batch_size)}
                """,
                (source, dimension_type, build_marker),
            )
            deleted = int(cur.rowcount)
        conn.commit()
        stale_deleted += deleted
        if deleted == 0:
            return stale_deleted
