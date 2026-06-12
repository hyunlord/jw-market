"""Local DB helpers for the new ETL file-manifest gate.

This module is deliberately narrow in phase 1B: it may create and use only
``ingest_manifest`` in the local ``jw_mart`` database. It does not read or write
serving, mart, raw, or cache tables. The production/Galera path is intentionally
absent because s0 is a local preflight gate.

``period`` and ``row_count`` are nullable because s0 only fingerprints files.
It reads bytes for SHA-256 but does not parse workbook/CSV contents. Period
coverage and row counts belong to s1 load, where source contents are actually
opened and interpreted.
"""
from __future__ import annotations

import os
from collections.abc import Iterable
from typing import Any

import pymysql
from pymysql.connections import Connection


def connect_local() -> Connection:
    """Connect to local jw_mart using DB_ROOT_PASSWORD.

    The helper intentionally defaults to 127.0.0.1:3308/jw_mart and root so the
    preflight cannot accidentally point at GCP/Galera. Passwords are read only
    from the environment and must never be logged.
    """
    password = os.environ.get("DB_ROOT_PASSWORD")
    if not password:
        raise RuntimeError("DB_ROOT_PASSWORD is required for local ingest_manifest checks.")
    return pymysql.connect(
        host="127.0.0.1",
        port=3308,
        user="root",
        password=password,
        database="jw_mart",
        charset="utf8mb4",
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


def ensure_manifest_table(conn: Connection) -> None:
    """Create ``ingest_manifest`` if needed, without touching any other table."""
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS ingest_manifest (
              id          BIGINT AUTO_INCREMENT PRIMARY KEY,
              source      VARCHAR(32)  NOT NULL,
              file_name   VARCHAR(512) NOT NULL,
              file_hash   CHAR(64)     NOT NULL,
              file_size   BIGINT       NOT NULL,
              mtime       DOUBLE       NOT NULL,
              period      VARCHAR(16)  NULL COMMENT 's0 is file-only; s1 load fills period after parsing source content',
              row_count   BIGINT       NULL COMMENT 's0 does not parse rows; s1 load fills row_count after loading',
              run_id      VARCHAR(64)  NOT NULL,
              recorded_at DATETIME     NOT NULL,
              KEY idx_source_file (source, file_name)
            )
            """
        )


def latest_manifest(conn: Connection) -> dict[tuple[str, str], dict[str, Any]]:
    """Return rows for the latest recorded run_id keyed by ``(source, file_name)``."""
    with conn.cursor() as cur:
        cur.execute("SELECT run_id FROM ingest_manifest ORDER BY recorded_at DESC, id DESC LIMIT 1")
        latest = cur.fetchone()
        if not latest:
            return {}
        cur.execute(
            """
            SELECT source, file_name, file_hash, file_size, mtime, period, row_count, run_id, recorded_at
            FROM ingest_manifest
            WHERE run_id = %s
            ORDER BY source, file_name
            """,
            (latest["run_id"],),
        )
        rows = cur.fetchall()
    return {(row["source"], row["file_name"]): row for row in rows}


def record_manifest(conn: Connection, rows: Iterable[dict[str, Any]], run_id: str) -> None:
    """Record the current file manifest for a manual ``--record-baseline`` run.

    Automatic recording belongs after successful load/cache completion. Because
    s1-s6 are still stubs in phase 1B, recording is manual and explicit here.
    ``period`` and ``row_count`` remain NULL by design.
    """
    payload = [
        (
            row["source"],
            row["file_name"],
            row["file_hash"],
            row["file_size"],
            row["mtime"],
            run_id,
            row["recorded_at"],
        )
        for row in rows
    ]
    if not payload:
        return
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO ingest_manifest
              (source, file_name, file_hash, file_size, mtime, period, row_count, run_id, recorded_at)
            VALUES
              (%s, %s, %s, %s, %s, NULL, NULL, %s, %s)
            """,
            payload,
        )
