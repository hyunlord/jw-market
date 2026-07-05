from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any

import pymysql

from pipeline.scripts.api.config import get_settings


def init_pool() -> None:
    """Placeholder for future pooling; kept for FastAPI lifespan symmetry."""


def close_pool() -> None:
    """Placeholder for future pooling; kept for FastAPI lifespan symmetry."""


def connect() -> pymysql.connections.Connection:
    settings = get_settings()
    return pymysql.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        database=settings.db_name,
        charset="utf8mb4",
        autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
    )


def fetch_all(sql: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            return list(cur.fetchall())


def iter_rows(sql: str, params: Sequence[Any] | None = None, *, batch_size: int = 500) -> Iterator[dict[str, Any]]:
    settings = get_settings()
    with pymysql.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        database=settings.db_name,
        charset="utf8mb4",
        autocommit=False,
        cursorclass=pymysql.cursors.SSDictCursor,
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            while True:
                rows = cur.fetchmany(batch_size)
                if not rows:
                    break
                yield from rows


def fetch_one(sql: str, params: Sequence[Any] | None = None) -> dict[str, Any] | None:
    rows = fetch_all(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: Sequence[Any] | None = None) -> int:
    with connect() as conn:
        with conn.cursor() as cur:
            affected = cur.execute(sql, params or ())
        conn.commit()
    return affected
