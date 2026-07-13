from __future__ import annotations

from typing import Any

import pymysql

from pipeline.etl.io.mart.filter_dimension_load import quote_id
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
) -> dict[str, Any]:
    """Promote one verified sidecar slice without touching adjacent dimensions."""

    guard_dimension_stage_target(source_db)
    if not allow_shared_serving_target:
        raise ValueError("shared serving target promotion requires explicit approval")
    if source != "ubist" or dimension_type != "molecule":
        raise ValueError("this promotion path is restricted to the ubist/molecule slice")
    if batch_size < 1 or batch_size > 200:
        raise ValueError("batch_size must be between 1 and 200")
    if "`" in target_db or not target_db.replace("_", "").isalnum():
        raise ValueError(f"unsafe target schema name: {target_db}")

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
    }


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
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT COUNT(*) AS n FROM {target_table} "
            "WHERE source=%s AND dimension_type=%s AND computed_at=%s",
            (source, dimension_type, build_marker),
        )
        marked = int(cur.fetchone()["n"])
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
