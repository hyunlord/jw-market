from __future__ import annotations

"""Bounded-copy helpers for filter-dimension promotion tables."""

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
import hashlib
import json
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


@dataclass(frozen=True, slots=True)
class TableCopyProof:
    row_count: int
    source_sha256: str
    target_sha256: str


def copy_table_consistent_snapshot(
    snapshot_conn: pymysql.connections.Connection,
    writer_conn: pymysql.connections.Connection,
    *,
    source_table: str,
    target_table: str,
    batch_size: int,
) -> TableCopyProof:
    """Copy one point-in-time source view through bounded writer commits."""

    if batch_size < 1 or batch_size > 200:
        raise ValueError("batch_size must be between 1 and 200")

    try:
        with snapshot_conn.cursor() as cur:
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            cur.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")
        columns = _table_columns(snapshot_conn, source_table)
        source_count, source_sha256 = _copy_snapshot_rows(
            snapshot_conn,
            writer_conn,
            source_table=source_table,
            target_table=target_table,
            columns=columns,
            batch_size=batch_size,
        )
        target_count, target_sha256 = _table_sha256(
            writer_conn,
            target_table,
            columns=columns,
            batch_size=batch_size,
        )
        if (
            source_count < 1
            or source_count != target_count
            or source_sha256 != target_sha256
        ):
            raise RuntimeError(
                "FDM consistent snapshot integrity mismatch: "
                f"source_rows={source_count} target_rows={target_count} "
                f"source_sha256={source_sha256} target_sha256={target_sha256}"
            )
        return TableCopyProof(
            row_count=source_count,
            source_sha256=source_sha256,
            target_sha256=target_sha256,
        )
    except Exception as exc:
        cleanup_error = _discard_partial_copy(writer_conn, target_table)
        if cleanup_error is not None:
            raise RuntimeError(
                "FDM consistent snapshot copy failed and automatic cleanup failed: "
                f"copy={exc!r}; cleanup={cleanup_error!r}"
            ) from cleanup_error
        raise RuntimeError(
            f"FDM consistent snapshot copy failed; partial target removed: {exc}"
        ) from exc
    finally:
        snapshot_conn.rollback()


def create_filter_dimension_backup_batched(
    conn: pymysql.connections.Connection,
    snapshot_conn: pymysql.connections.Connection,
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
    with conn.cursor() as cur:
        cur.execute(
            f"CREATE TABLE {names.qualified_backup} LIKE {names.qualified_live}"
        )
    proof = copy_table_consistent_snapshot(
        snapshot_conn,
        conn,
        source_table=names.qualified_live,
        target_table=names.qualified_backup,
        batch_size=batch_size,
    )
    return {
        "table": names.backup_table,
        "row_count": proof.row_count,
        "source_sha256": proof.source_sha256,
        "backup_sha256": proof.target_sha256,
        "promotion_run_id": promotion_run_id,
    }


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


def _table_columns(
    conn: pymysql.connections.Connection,
    qualified_table: str,
) -> tuple[str, ...]:
    with conn.cursor() as cur:
        cur.execute(f"SHOW COLUMNS FROM {qualified_table}")
        columns = tuple(str(row["Field"]) for row in cur.fetchall())
    if not columns or "id" not in columns:
        raise RuntimeError(f"FDM copy source lacks an id-bearing schema: {qualified_table}")
    return columns


def _copy_snapshot_rows(
    snapshot_conn: pymysql.connections.Connection,
    writer_conn: pymysql.connections.Connection,
    *,
    source_table: str,
    target_table: str,
    columns: tuple[str, ...],
    batch_size: int,
) -> tuple[int, str]:
    rendered = ", ".join(quote_id(column) for column in columns)
    placeholders = ", ".join(["%s"] * len(columns))
    insert_sql = f"INSERT INTO {target_table} ({rendered}) VALUES ({placeholders})"
    digest = _new_table_digest(columns)
    copied = 0
    last_id = 0
    while True:
        with snapshot_conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {rendered}
                FROM {source_table}
                WHERE id > %s
                ORDER BY id
                LIMIT {int(batch_size)}
                """,
                (last_id,),
            )
            rows = list(cur.fetchall())
        if not rows:
            break
        _update_table_digest(digest, rows, columns)
        payloads = [tuple(row[column] for column in columns) for row in rows]
        with writer_conn.cursor() as cur:
            cur.executemany(insert_sql, payloads)
            inserted = int(cur.rowcount)
        if inserted != len(rows):
            raise RuntimeError(
                f"FDM snapshot batch short write: inserted={inserted} expected={len(rows)}"
            )
        writer_conn.commit()
        copied += inserted
        next_id = int(rows[-1]["id"])
        if next_id <= last_id:
            raise RuntimeError("FDM snapshot cursor did not advance")
        last_id = next_id
    return copied, digest.hexdigest()


def _table_sha256(
    conn: pymysql.connections.Connection,
    qualified_table: str,
    *,
    columns: tuple[str, ...],
    batch_size: int,
) -> tuple[int, str]:
    rendered = ", ".join(quote_id(column) for column in columns)
    digest = _new_table_digest(columns)
    row_count = 0
    last_id = 0
    while True:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {rendered}
                FROM {qualified_table}
                WHERE id > %s
                ORDER BY id
                LIMIT {int(batch_size)}
                """,
                (last_id,),
            )
            rows = list(cur.fetchall())
        if not rows:
            break
        _update_table_digest(digest, rows, columns)
        row_count += len(rows)
        next_id = int(rows[-1]["id"])
        if next_id <= last_id:
            raise RuntimeError("FDM checksum cursor did not advance")
        last_id = next_id
    return row_count, digest.hexdigest()


def _new_table_digest(columns: tuple[str, ...]) -> Any:
    digest = hashlib.sha256()
    digest.update(
        json.dumps(columns, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    digest.update(b"\n")
    return digest


def _update_table_digest(
    digest: Any,
    rows: list[dict[str, Any]],
    columns: tuple[str, ...],
) -> None:
    for row in rows:
        payload = [_canonical_db_value(row[column]) for column in columns]
        digest.update(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        )
        digest.update(b"\n")


def _canonical_db_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, bytes):
        return {"bytes_hex": value.hex()}
    if isinstance(value, Decimal):
        return {"decimal": str(value)}
    if isinstance(value, (datetime, date, time)):
        return {"iso8601": value.isoformat()}
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("non-finite database value cannot be checksummed")
        return {"float": repr(value)}
    return {"text": str(value)}


def _discard_partial_copy(
    writer_conn: pymysql.connections.Connection,
    target_table: str,
) -> Exception | None:
    try:
        writer_conn.rollback()
        with writer_conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {target_table}")
        writer_conn.commit()
    except Exception as exc:
        return exc
    return None
