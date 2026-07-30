from __future__ import annotations

"""Atomic table-swap primitives for the general filter-dimension sidecar."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pymysql

from pipeline.etl.io.mart.filter_dimension_copy import (
    copy_table_batched,
    create_filter_dimension_backup_batched,
    qualified,
    require_identifier_length,
    swap_names,
    table_count,
    table_exists,
)
from pipeline.etl.io.mart.filter_dimension_metric import FILTER_DIMENSION_TABLE


@dataclass(frozen=True, slots=True)
class FilterDimensionSwap:
    target_db: str
    live_table: str
    stage_table: str
    backup_table: str
    live_rows: int

    @property
    def qualified_live(self) -> str:
        return qualified(self.target_db, self.live_table)

    @property
    def qualified_stage(self) -> str:
        return qualified(self.target_db, self.stage_table)

    @property
    def qualified_backup(self) -> str:
        return qualified(self.target_db, self.backup_table)


def prepare_filter_dimension_swap(
    conn: pymysql.connections.Connection,
    target_db: str,
    promotion_run_id: str,
    *,
    batch_size: int,
) -> FilterDimensionSwap:
    """Populate a hidden full-table candidate in bounded commits."""

    names = swap_names(target_db, promotion_run_id)
    for table_name, role in (
        (names.stage_table, "stage"),
        (names.backup_table, "backup"),
    ):
        if table_exists(conn, target_db, table_name):
            raise RuntimeError(
                f"FDM {role} already exists: {target_db}.{table_name}"
            )
    live_rows = table_count(conn, names.qualified_live)
    if live_rows < 1:
        raise RuntimeError(
            f"refusing to promote over empty FDM table: {target_db}.{FILTER_DIMENSION_TABLE}"
        )
    with conn.cursor() as cur:
        cur.execute(
            f"CREATE TABLE {names.qualified_stage} LIKE {names.qualified_live}"
        )
    try:
        copied = copy_table_batched(
            conn,
            source_table=names.qualified_live,
            target_table=names.qualified_stage,
            expected_rows=live_rows,
            batch_size=batch_size,
        )
    except (pymysql.MySQLError, RuntimeError) as exc:
        raise RuntimeError(
            f"FDM stage incomplete: expected={live_rows} "
            f"table={target_db}.{names.stage_table}; scratch retained for "
            f"inspection and same-run retry is blocked; cleanup after verification: "
            f"DROP TABLE {names.qualified_stage}"
        ) from exc
    if copied != live_rows:
        raise RuntimeError(
            f"FDM stage incomplete: copied={copied} expected={live_rows} "
            f"table={target_db}.{names.stage_table}"
        )
    return FilterDimensionSwap(
        target_db=target_db,
        live_table=names.live_table,
        stage_table=names.stage_table,
        backup_table=names.backup_table,
        live_rows=live_rows,
    )


def activate_filter_dimension_swap(
    conn: pymysql.connections.Connection,
    swap: FilterDimensionSwap,
    *,
    source: str,
    on_activated: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Expose the completed candidate and retain the prior live table."""

    with conn.cursor() as cur:
        cur.execute(
            "RENAME TABLE "
            f"{swap.qualified_live} TO {swap.qualified_backup}, "
            f"{swap.qualified_stage} TO {swap.qualified_live}"
        )
    try:
        backup_rows = table_count(conn, swap.qualified_backup)
        if backup_rows != swap.live_rows:
            raise RuntimeError(
                f"FDM atomic backup row count mismatch: "
                f"{backup_rows} != {swap.live_rows}"
            )
        invalidated = invalidate_dynamic_market_cache(
            conn,
            swap.target_db,
            source=source,
        )
        result = {
            "table": swap.backup_table,
            "row_count": backup_rows,
            "promotion_run_id": _run_id_from_backup(swap.backup_table),
            "cache_rows_invalidated": invalidated,
        }
        if on_activated is not None:
            on_activated(result)
        return result
    except Exception as exc:
        try:
            conn.rollback()
            _restore_failed_activation(conn, swap)
        except Exception as restore_exc:
            raise RuntimeError(
                "FDM activation failed after atomic swap and automatic live-table "
                f"restoration also failed: activation={exc!r}; "
                f"restoration={restore_exc!r}"
            ) from restore_exc
        raise RuntimeError(
            "FDM activation failed after atomic swap; previous live table restored"
        ) from exc


def rollback_filter_dimension_swap(
    conn: pymysql.connections.Connection,
    *,
    target_db: str,
    promotion_run_id: str,
    expected_backup_rows: int,
) -> dict[str, Any]:
    """Restore one FDM backup without requiring a four-component generation."""

    names = swap_names(target_db, promotion_run_id)
    failed_table = f"{FILTER_DIMENSION_TABLE}__failed_{promotion_run_id}"
    require_identifier_length(failed_table, "rollback scratch")
    if not table_exists(conn, target_db, names.live_table):
        raise RuntimeError(f"FDM live table is missing: {target_db}.{names.live_table}")
    if not table_exists(conn, target_db, names.backup_table):
        raise RuntimeError(
            f"FDM rollback backup is missing: {target_db}.{names.backup_table}"
        )
    if table_exists(conn, target_db, failed_table):
        raise RuntimeError(
            f"FDM rollback scratch already exists: {target_db}.{failed_table}"
        )
    backup_rows = table_count(conn, names.qualified_backup)
    if backup_rows != expected_backup_rows or backup_rows < 1:
        raise RuntimeError(
            f"FDM rollback backup row count mismatch: "
            f"{backup_rows} != {expected_backup_rows}"
        )
    qualified_failed = qualified(target_db, failed_table)
    with conn.cursor() as cur:
        cur.execute(
            "RENAME TABLE "
            f"{names.qualified_live} TO {qualified_failed}, "
            f"{names.qualified_backup} TO {names.qualified_live}"
        )
    restored_rows = table_count(conn, names.qualified_live)
    if restored_rows != expected_backup_rows:
        raise RuntimeError(
            f"FDM post-rollback row count mismatch: "
            f"{restored_rows} != {expected_backup_rows}"
        )
    invalidated = invalidate_dynamic_market_cache(
        conn,
        target_db,
        source="ubist",
    )
    return {
        "promotion_run_id": promotion_run_id,
        "restored_rows": restored_rows,
        "failed_table": failed_table,
        "cache_rows_invalidated": invalidated,
    }


def invalidate_dynamic_market_cache(
    conn: pymysql.connections.Connection,
    target_db: str,
    *,
    source: str,
) -> int:
    """Delete only dynamic responses owned by the promoted source."""

    cache = qualified(target_db, "cache_dynamic_market_response")
    with conn.cursor() as cur:
        cur.execute(
            f"""
            DELETE FROM {cache}
            WHERE JSON_UNQUOTE(JSON_EXTRACT(request_json, '$.source'))=%s
            """,
            (source,),
        )
        invalidated = int(cur.rowcount)
    conn.commit()
    return invalidated


def _restore_failed_activation(
    conn: pymysql.connections.Connection,
    swap: FilterDimensionSwap,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "RENAME TABLE "
            f"{swap.qualified_live} TO {swap.qualified_stage}, "
            f"{swap.qualified_backup} TO {swap.qualified_live}"
        )


def _run_id_from_backup(backup_table: str) -> str:
    prefix = f"{FILTER_DIMENSION_TABLE}__old_"
    return backup_table.removeprefix(prefix)
