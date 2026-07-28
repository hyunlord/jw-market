"""Strict provenance persistence for CSD publications."""
from __future__ import annotations

import hashlib
from typing import Protocol

from pipeline.scripts.ingest_hook.csd_publication import (
    PublicationPlan,
    PublicationRecord,
)


PROVENANCE_TABLE = "mart_publication_provenance"


class Cursor(Protocol):
    rowcount: int

    def execute(self, statement: str, parameters: tuple = ()) -> None: ...
    def fetchone(self) -> tuple | None: ...
    def fetchall(self) -> tuple[tuple, ...]: ...
    def close(self) -> None: ...


class Connection(Protocol):
    def cursor(self) -> Cursor: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...


def quote_id(value: str) -> str:
    return f"`{value}`"


def bounded_id(*parts: str) -> str:
    """Return a deterministic MariaDB identifier no longer than 64 characters."""
    value = "_".join(part for part in parts if part)
    if len(value) <= 64:
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"{value[:51]}_{digest}"


def table_exists(cursor: Cursor, schema: str, table: str) -> bool:
    cursor.execute(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema = %s AND table_name = %s",
        (schema, table),
    )
    row = cursor.fetchone()
    return row is not None and int(row[0]) == 1


def record(
    conn: Connection,
    *,
    live_stage: str,
    plan: PublicationPlan,
    publication: PublicationRecord,
) -> None:
    q = quote_id
    cursor = conn.cursor()
    try:
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {q(live_stage)}.{q(PROVENANCE_TABLE)} (
              publication_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
              category VARCHAR(32) NOT NULL,
              epoch VARCHAR(32) NOT NULL,
              run_id VARCHAR(64) NOT NULL,
              input_inventory_sha256 CHAR(64) NOT NULL,
              builder_commit CHAR(40) NOT NULL,
              image_digest VARCHAR(80) NOT NULL,
              window_start CHAR(7) NOT NULL,
              window_end CHAR(7) NOT NULL,
              published_at_utc VARCHAR(40) NOT NULL,
              published_at_kst VARCHAR(40) NOT NULL,
              UNIQUE KEY uq_csd_publication_run (category, run_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        cursor.execute(
            f"INSERT INTO {q(live_stage)}.{q(PROVENANCE_TABLE)} "
            "(category, epoch, run_id, input_inventory_sha256, builder_commit, "
            "image_digest, window_start, window_end, published_at_utc, published_at_kst) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                plan.category,
                plan.epoch,
                plan.run_id,
                publication.inventory_sha256,
                publication.builder_commit,
                publication.image_digest,
                publication.window_start,
                publication.window_end,
                publication.published_at_utc,
                publication.published_at_kst,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()


def delete_if_present(
    cursor: Cursor,
    *,
    live_stage: str,
    category: str,
    run_id: str,
) -> None:
    if not table_exists(cursor, live_stage, PROVENANCE_TABLE):
        return
    cursor.execute(
        f"DELETE FROM {quote_id(live_stage)}.{quote_id(PROVENANCE_TABLE)} "
        "WHERE category = %s AND run_id = %s",
        (category, run_id),
    )
