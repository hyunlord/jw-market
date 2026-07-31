from __future__ import annotations

"""Atomic table-swap primitives for the general filter-dimension sidecar."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pymysql

from pipeline.etl.io.mart.filter_dimension_copy import (
    copy_table_consistent_snapshot,
    create_filter_dimension_backup_batched,
    qualified,
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
    baseline_source_sha256: str
    baseline_stage_sha256: str

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
    snapshot_conn: pymysql.connections.Connection,
    target_db: str,
    promotion_run_id: str,
    *,
    batch_size: int,
    on_progress: Callable[[str, dict[str, Any]], None] | None = None,
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
    with conn.cursor() as cur:
        cur.execute(
            f"CREATE TABLE {names.qualified_stage} LIKE {names.qualified_live}"
        )
    proof = copy_table_consistent_snapshot(
        snapshot_conn,
        conn,
        source_table=names.qualified_live,
        target_table=names.qualified_stage,
        batch_size=batch_size,
        on_progress=on_progress,
    )
    return FilterDimensionSwap(
        target_db=target_db,
        live_table=names.live_table,
        stage_table=names.stage_table,
        backup_table=names.backup_table,
        live_rows=proof.row_count,
        baseline_source_sha256=proof.source_sha256,
        baseline_stage_sha256=proof.target_sha256,
    )


def activate_filter_dimension_swap(
    conn: pymysql.connections.Connection,
    swap: FilterDimensionSwap,
    *,
    source: str,
    on_activated: Callable[[dict[str, Any]], None] | None = None,
    on_progress: Callable[[str, dict[str, Any]], None] | None = None,
    on_compensated: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Expose the completed candidate and retain the prior live table."""

    renamed = False
    try:
        if on_progress is not None:
            on_progress("activation_prepared", {})
        with conn.cursor() as cur:
            cur.execute(
                "RENAME TABLE "
                f"{swap.qualified_live} TO {swap.qualified_backup}, "
                f"{swap.qualified_stage} TO {swap.qualified_live}"
            )
        renamed = True
        if on_progress is not None:
            on_progress("activated", {})
        backup_rows = table_count(conn, swap.qualified_backup)
        if backup_rows != swap.live_rows:
            raise RuntimeError(
                f"FDM atomic backup row count mismatch: "
                f"{backup_rows} != {swap.live_rows}"
            )
        result = {
            "table": swap.backup_table,
            "row_count": backup_rows,
            "promotion_run_id": _run_id_from_backup(swap.backup_table),
            "baseline_source_sha256": swap.baseline_source_sha256,
            "baseline_stage_sha256": swap.baseline_stage_sha256,
        }
        if on_activated is not None:
            on_activated(result)
        if on_progress is not None:
            on_progress("component_recorded", {})
        invalidated = invalidate_dynamic_market_cache(
            conn,
            swap.target_db,
            source=source,
        )
        if on_progress is not None:
            on_progress(
                "cache_invalidated",
                {"rows_affected": invalidated},
            )
        result["cache_rows_invalidated"] = invalidated
        if on_progress is not None:
            on_progress("completed", {})
        return result
    except Exception as exc:
        try:
            conn.rollback()
            if renamed:
                _restore_failed_activation(conn, swap)
            _discard_failed_candidate(conn, swap)
            if on_compensated is not None:
                on_compensated()
            if on_progress is not None:
                on_progress("compensated", {"last_error": repr(exc)})
        except Exception as restore_exc:
            raise RuntimeError(
                "FDM activation failed after atomic swap and automatic live-table "
                f"restoration also failed: activation={exc!r}; "
                f"restoration={restore_exc!r}"
            ) from restore_exc
        if renamed:
            raise RuntimeError(
                "FDM activation failed after atomic swap; previous live table restored"
            ) from exc
        raise RuntimeError(
            "FDM activation failed before atomic swap; candidate removed"
        ) from exc


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


def _discard_failed_candidate(
    conn: pymysql.connections.Connection,
    swap: FilterDimensionSwap,
) -> None:
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {swap.qualified_stage}")
    conn.commit()


def _run_id_from_backup(backup_table: str) -> str:
    prefix = f"{FILTER_DIMENSION_TABLE}__old_"
    return backup_table.removeprefix(prefix)
