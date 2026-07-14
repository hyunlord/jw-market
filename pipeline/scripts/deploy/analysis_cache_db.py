"""Lightweight database helpers for runtime cache publication."""

from __future__ import annotations

import os
import re

import pymysql

from pipeline.scripts.deploy.mart_load_verify import quote_id


SCHEMA_RE = re.compile(r"^[A-Za-z0-9_]+$")


def connect_admin() -> pymysql.connections.Connection:
    root_password = os.environ.get("MARIADB_ROOT_PASSWORD")
    password = root_password or os.environ.get("MARIADB_PASSWORD") or os.environ.get("DB_PASSWORD")
    user = "root" if root_password else os.environ.get("MARIADB_USER") or os.environ.get("DB_USER", "jwapp")
    if not password:
        raise RuntimeError("MARIADB_ROOT_PASSWORD/MARIADB_PASSWORD/DB_PASSWORD is missing")
    return pymysql.connect(
        host=os.environ.get("MARIADB_HOST") or os.environ.get("DB_HOST", "127.0.0.1"),
        port=int(
            os.environ.get("MARIADB_PORT")
            or os.environ.get("DB_PORT")
            or os.environ.get("HOST_PORT", "3307")
        ),
        user=user,
        password=password,
        charset="utf8mb4",
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


def validate_schema_name(label: str, db_name: str) -> None:
    if not SCHEMA_RE.fullmatch(db_name):
        raise ValueError(f"{label} must contain only letters, numbers, and underscores: {db_name!r}")
    blocked = {"mysql", "information_schema", "performance_schema", "sys"}
    if db_name.lower() in blocked:
        raise ValueError(f"{label} points at a system schema: {db_name}")


def table_row_count(
    conn: pymysql.connections.Connection,
    db_name: str,
    table_name: str,
) -> int:
    with conn.cursor() as cursor:
        cursor.execute(f"SELECT COUNT(*) AS row_count FROM {quote_id(db_name)}.{quote_id(table_name)}")
        row = cursor.fetchone()
    return int(row["row_count"])
