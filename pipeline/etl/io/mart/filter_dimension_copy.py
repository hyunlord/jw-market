from __future__ import annotations

"""Bounded-copy helpers for filter-dimension promotion tables."""

from dataclasses import dataclass
import re
from typing import Any

import pymysql

from pipeline.etl.io.mart.filter_dimension_load import quote_id
from pipeline.etl.io.mart.filter_dimension_metric import FILTER_DIMENSION_TABLE


_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_]+$")


@dataclass(frozen=True, slots=True)
class FilterDimensionNames:
    target_db: str
    live_table: str
    stage_table: str
    backup_table: str

    @property
    def qualified_live(self) -> str:
        return qualified(self.target_db, self.live_table)

    @property
    def qualified_stage(self) -> str:
        return qualified(self.target_db, self.stage_table)

    @property
    def qualified_backup(self) -> str:
        return qualified(self.target_db, self.backup_table)


def create_filter_dimension_backup_batched(
    conn: pymysql.connections.Connection,
    target_db: str,
    promotion_run_id: str,
    *,
    batch_size: int,
) -> dict[str, Any]:
    """Create a complete rollback copy without a cluster-sized writeset."""

    names = swap_names(target_db, promotion_run_id)
    if table_exists(conn, target_db, names.backup_table):
        raise RuntimeError(
            f"FDM backup already exists: {target_db}.{names.backup_table}"
        )
    live_rows = table_count(conn, names.qualified_live)
    if live_rows < 1:
        raise RuntimeError(
            f"refusing to back up empty FDM table: "
            f"{target_db}.{FILTER_DIMENSION_TABLE}"
        )
    with conn.cursor() as cur:
        cur.execute(
            f"CREATE TABLE {names.qualified_backup} LIKE {names.qualified_live}"
        )
    try:
        copied = copy_table_batched(
            conn,
            source_table=names.qualified_live,
            target_table=names.qualified_backup,
            expected_rows=live_rows,
            batch_size=batch_size,
        )
    except (pymysql.MySQLError, RuntimeError) as exc:
        raise RuntimeError(
            f"FDM backup incomplete: expected={live_rows} "
            f"table={target_db}.{names.backup_table}; scratch retained for "
            f"inspection and same-run retry is blocked; cleanup after verification: "
            f"DROP TABLE {names.qualified_backup}"
        ) from exc
    backup_rows = table_count(conn, names.qualified_backup)
    if copied != live_rows or backup_rows != live_rows:
        raise RuntimeError(
            f"FDM backup row count mismatch: copied={copied} "
            f"backup={backup_rows} live={live_rows}"
        )
    return {
        "table": names.backup_table,
        "row_count": backup_rows,
        "promotion_run_id": promotion_run_id,
    }


def copy_table_batched(
    conn: pymysql.connections.Connection,
    *,
    source_table: str,
    target_table: str,
    expected_rows: int,
    batch_size: int,
) -> int:
    copied = 0
    last_id = 0
    while copied < expected_rows:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {target_table}
                SELECT *
                FROM {source_table}
                WHERE id > %s
                ORDER BY id
                LIMIT {int(batch_size)}
                """,
                (last_id,),
            )
            inserted = int(cur.rowcount)
        if inserted == 0:
            break
        conn.commit()
        copied += inserted
        last_id = _last_copied_id(
            conn,
            source_table,
            after_id=last_id,
            copied_rows=inserted,
        )
    staged_rows = table_count(conn, target_table)
    if copied != expected_rows or staged_rows != expected_rows:
        raise RuntimeError(
            f"FDM staged table row count mismatch: "
            f"copied={copied} staged={staged_rows} expected={expected_rows}"
        )
    return copied


def swap_names(target_db: str, promotion_run_id: str) -> FilterDimensionNames:
    if not _RUN_ID_RE.fullmatch(promotion_run_id):
        raise ValueError(
            "promotion_run_id must contain only letters, numbers, and underscores"
        )
    stage_table = f"{FILTER_DIMENSION_TABLE}__stage_{promotion_run_id}"
    backup_table = f"{FILTER_DIMENSION_TABLE}__old_{promotion_run_id}"
    require_identifier_length(stage_table, "stage")
    require_identifier_length(backup_table, "backup")
    return FilterDimensionNames(
        target_db=target_db,
        live_table=FILTER_DIMENSION_TABLE,
        stage_table=stage_table,
        backup_table=backup_table,
    )


def require_identifier_length(table_name: str, role: str) -> None:
    if len(table_name) > 64:
        raise ValueError(f"FDM {role} identifier exceeds 64 characters: {table_name}")


def table_exists(
    conn: pymysql.connections.Connection,
    db_name: str,
    table_name: str,
) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS table_count FROM information_schema.tables "
            "WHERE table_schema=%s AND table_name=%s",
            (db_name, table_name),
        )
        return int(cur.fetchone()["table_count"]) > 0


def table_count(
    conn: pymysql.connections.Connection,
    qualified_table: str,
) -> int:
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS n FROM {qualified_table}")
        return int(cur.fetchone()["n"])


def qualified(db_name: str, table_name: str) -> str:
    return f"{quote_id(db_name)}.{quote_id(table_name)}"


def _last_copied_id(
    conn: pymysql.connections.Connection,
    source_table: str,
    *,
    after_id: int,
    copied_rows: int,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT MAX(id) AS max_id
            FROM (
                SELECT id
                FROM {source_table}
                WHERE id > %s
                ORDER BY id
                LIMIT {int(copied_rows)}
            ) AS copied_rows
            """,
            (after_id,),
        )
        row = cur.fetchone()
    max_id = row["max_id"]
    if max_id is None:
        raise RuntimeError("FDM batch copy advanced without a source id")
    return int(max_id)
