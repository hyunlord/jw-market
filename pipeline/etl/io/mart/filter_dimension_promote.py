from __future__ import annotations

from collections.abc import Callable, Sequence
import time
from typing import Any

import pymysql

from pipeline.contracts.dimension_registry import dimension_value_hash
from pipeline.etl.io.mart.filter_dimension_load import quote_id
from pipeline.etl.io.mart.general_json import dumps
from pipeline.etl.io.mart.filter_dimension_metric import FILTER_DIMENSION_TABLE
from pipeline.etl.io.mart.filter_dimension_metric import guard_dimension_stage_target
from pipeline.etl.io.mart.filter_dimension_copy import (
    create_filter_dimension_backup_batched,
)
from pipeline.etl.io.mart.filter_dimension_swap import (
    FilterDimensionSwap,
    activate_filter_dimension_swap,
    prepare_filter_dimension_swap,
)


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


def promote_filter_dimension_rows(
    conn: pymysql.connections.Connection,
    rows: Sequence[dict[str, Any]],
    target_db: str,
    *,
    snapshot_conn: pymysql.connections.Connection,
    source: str,
    dimension_type: str,
    build_marker: str,
    batch_size: int = 200,
    allow_shared_serving_target: bool = False,
    promotion_run_id: str,
    on_activated: Callable[[dict[str, Any]], None] | None = None,
    on_progress: Callable[[str, dict[str, Any]], None] | None = None,
    on_compensated: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Promote a fully computed slice through a hidden atomic-swap table."""

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

    if on_progress is not None:
        on_progress("started", {})
    swap = prepare_filter_dimension_swap(
        conn,
        snapshot_conn,
        target_db,
        promotion_run_id,
        batch_size=batch_size,
        on_progress=on_progress,
    )
    activation_started = False
    try:
        promoted = _upsert_payloads(
            conn,
            target_table=swap.qualified_stage,
            payloads=payloads,
            batch_size=batch_size,
            on_progress=on_progress,
        )
        expected = len(payloads)
        _require_complete_promotion(
            conn,
            target_table=swap.qualified_stage,
            source=source,
            dimension_type=dimension_type,
            build_marker=build_marker,
            expected=expected,
            promoted=promoted,
        )
        stale_deleted = _delete_stale_slice(
            conn,
            target_table=swap.qualified_stage,
            source=source,
            dimension_type=dimension_type,
            build_marker=build_marker,
            batch_size=batch_size,
            on_progress=on_progress,
        )
        activation_started = True
        backup = activate_filter_dimension_swap(
            conn,
            swap,
            source=source,
            on_activated=on_activated,
            on_progress=on_progress,
            on_compensated=on_compensated,
        )
    except Exception as exc:
        if not activation_started:
            _discard_unactivated_candidate(conn, swap)
            if on_progress is not None:
                on_progress("compensated", {"last_error": repr(exc)})
        raise
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
    snapshot_conn: pymysql.connections.Connection,
    source: str,
    dimension_type: str,
    build_marker: str,
    batch_size: int = 200,
    allow_shared_serving_target: bool = False,
    promotion_run_id: str,
    on_activated: Callable[[dict[str, Any]], None] | None = None,
    on_progress: Callable[[str, dict[str, Any]], None] | None = None,
    on_compensated: Callable[[], None] | None = None,
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
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT COUNT(*) AS n FROM {source_table} WHERE source=%s AND dimension_type=%s",
            (source, dimension_type),
        )
        expected = int(cur.fetchone()["n"])
    if expected < 1:
        raise RuntimeError("refusing to replace ubist/molecule with an empty staged slice")

    if on_progress is not None:
        on_progress("started", {})
    swap = prepare_filter_dimension_swap(
        conn,
        snapshot_conn,
        target_db,
        promotion_run_id,
        batch_size=batch_size,
        on_progress=on_progress,
    )
    activation_started = False
    try:
        promoted = _upsert_slice(
            conn,
            source_table=source_table,
            target_table=swap.qualified_stage,
            source=source,
            dimension_type=dimension_type,
            build_marker=build_marker,
            batch_size=batch_size,
            on_progress=on_progress,
        )
        _require_complete_promotion(
            conn,
            target_table=swap.qualified_stage,
            source=source,
            dimension_type=dimension_type,
            build_marker=build_marker,
            expected=expected,
            promoted=promoted,
        )
        stale_deleted = _delete_stale_slice(
            conn,
            target_table=swap.qualified_stage,
            source=source,
            dimension_type=dimension_type,
            build_marker=build_marker,
            batch_size=batch_size,
            on_progress=on_progress,
        )
        activation_started = True
        backup = activate_filter_dimension_swap(
            conn,
            swap,
            source=source,
            on_activated=on_activated,
            on_progress=on_progress,
            on_compensated=on_compensated,
        )
    except Exception as exc:
        if not activation_started:
            _discard_unactivated_candidate(conn, swap)
            if on_progress is not None:
                on_progress("compensated", {"last_error": repr(exc)})
        raise
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
    *,
    snapshot_conn: pymysql.connections.Connection,
    batch_size: int = 200,
) -> dict[str, Any]:
    _validate_batch_size(batch_size)
    return create_filter_dimension_backup_batched(
        conn,
        snapshot_conn,
        target_db,
        promotion_run_id,
        batch_size=batch_size,
    )


def _upsert_slice(
    conn: pymysql.connections.Connection,
    *,
    source_table: str,
    target_table: str,
    source: str,
    dimension_type: str,
    build_marker: str,
    batch_size: int,
    on_progress: Callable[[str, dict[str, Any]], None] | None,
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
        if on_progress is not None:
            on_progress(
                "slice_upsert_batch",
                {
                    "batch_index": (promoted + batch_size - 1) // batch_size,
                    "rows_affected": len(rows),
                    "rows_completed": promoted,
                },
            )
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
    _validate_batch_size(batch_size)
    if "`" in target_db or not target_db.replace("_", "").isalnum():
        raise ValueError(f"unsafe target schema name: {target_db}")


def _validate_batch_size(batch_size: int) -> None:
    if batch_size < 1 or batch_size > 200:
        raise ValueError("batch_size must be between 1 and 200")


def _promotion_payload(row: dict[str, Any], build_marker: str) -> tuple[Any, ...]:
    normalized = str(row["dimension_value_norm"])
    values = {
        **row,
        "dimension_value_hash": dimension_value_hash(normalized),
        "raw_value_history": dumps(row["raw_value_history"]),
    }
    return tuple(values[column] for column in _PROMOTION_COLUMNS) + (build_marker,)


def _upsert_payloads(
    conn: pymysql.connections.Connection,
    *,
    target_table: str,
    payloads: Sequence[tuple[Any, ...]],
    batch_size: int,
    on_progress: Callable[[str, dict[str, Any]], None] | None,
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
        if on_progress is not None:
            on_progress(
                "slice_upsert_batch",
                {
                    "batch_index": (promoted + batch_size - 1) // batch_size,
                    "rows_affected": len(batch),
                    "rows_completed": promoted,
                },
            )
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
    on_progress: Callable[[str, dict[str, Any]], None] | None,
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
        if deleted > 0 and on_progress is not None:
            on_progress(
                "slice_delete_batch",
                {
                    "batch_index": (
                        stale_deleted + batch_size - 1
                    ) // batch_size,
                    "rows_affected": deleted,
                    "rows_completed": stale_deleted,
                },
            )
        if deleted == 0:
            return stale_deleted


def _discard_unactivated_candidate(
    conn: pymysql.connections.Connection,
    swap: FilterDimensionSwap,
) -> None:
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {swap.qualified_stage}")
    conn.commit()
