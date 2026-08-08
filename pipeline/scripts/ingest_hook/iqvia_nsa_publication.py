"""UBIST-compatible publication provenance for IQVIA NSA."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from pipeline.scripts.deploy.mart_load_ops import (
    PublishAction,
    quote_id,
    restore_table_group_atomically,
)
from pipeline.scripts.rollback.ledger import PromotionLedger


PROVENANCE_TABLE = "mart_publication_provenance"
PUBLICATION_STATE_TABLE = "ingest_publication_state"
PUBLICATION_STATE_NAME = "normal_caches"
KST = timezone(timedelta(hours=9))


class PublicationConfig(Protocol):
    target_db: str
    builder_commit: str
    image_ref: str


@dataclass(frozen=True, slots=True)
class PublicationEvidence:
    inventory_sha256: str
    inventory_json: str
    window_start: str
    window_end: str


def build_publication_evidence(
    files: object,
    file_rows: dict[str, int],
    periods: tuple[str, ...],
) -> PublicationEvidence:
    ordered_files = sorted(
        (
            {
                "path": str(item.path),
                "rows": int(file_rows.get(str(item.path), item.rows or 0)),
                "sha256": str(item.sha256),
            }
            for item in files
        ),
        key=lambda item: (item["path"], item["sha256"]),
    )
    inventory_json = json.dumps(
        ordered_files,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    ordered_periods = tuple(sorted(set(periods)))
    if not ordered_periods:
        raise RuntimeError("publication provenance requires a non-empty NSA window")
    return PublicationEvidence(
        inventory_sha256=hashlib.sha256(inventory_json.encode("utf-8")).hexdigest(),
        inventory_json=inventory_json,
        window_start=ordered_periods[0],
        window_end=ordered_periods[-1],
    )


def _first_value(row: tuple[Any, ...] | dict[str, Any]) -> int:
    value = next(iter(row.values())) if isinstance(row, dict) else row[0]
    return int(value)


def _ensure_tables(cursor: Any, target_db: str) -> None:
    database = quote_id(target_db)
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {database}.{quote_id(PUBLICATION_STATE_TABLE)} (
          name VARCHAR(64) PRIMARY KEY,
          mart_publication_epoch BIGINT NOT NULL,
          category VARCHAR(32) NOT NULL,
          epoch VARCHAR(32) NOT NULL,
          run_id VARCHAR(64) NOT NULL,
          updated_at VARCHAR(32) NOT NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {database}.{quote_id(PROVENANCE_TABLE)} (
          mart_publication_epoch BIGINT PRIMARY KEY,
          category VARCHAR(32) NOT NULL,
          epoch VARCHAR(32) NOT NULL,
          run_id VARCHAR(64) NOT NULL,
          input_inventory_sha256 CHAR(64) NOT NULL,
          input_inventory_json LONGTEXT NOT NULL,
          builder_commit VARCHAR(64) NOT NULL,
          image_digest VARCHAR(255) NOT NULL,
          window_start VARCHAR(32) NOT NULL,
          window_end VARCHAR(32) NOT NULL,
          published_at_utc VARCHAR(40) NOT NULL,
          published_at_kst VARCHAR(40) NOT NULL,
          status VARCHAR(16) NOT NULL DEFAULT 'published',
          rolled_back_at_utc VARCHAR(40) NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    cursor.execute(
        f"ALTER TABLE {database}.{quote_id(PROVENANCE_TABLE)} "
        "ADD COLUMN IF NOT EXISTS status VARCHAR(16) NOT NULL DEFAULT 'published'"
    )
    cursor.execute(
        f"ALTER TABLE {database}.{quote_id(PROVENANCE_TABLE)} "
        "ADD COLUMN IF NOT EXISTS rolled_back_at_utc VARCHAR(40) NULL"
    )


def record_publication_provenance(
    conn: Any,
    config: PublicationConfig,
    *,
    run_id: str,
    epoch: str,
    evidence: PublicationEvidence,
) -> int:
    cursor = conn.cursor()
    now = datetime.now(timezone.utc)
    database = quote_id(config.target_db)
    state = f"{database}.{quote_id(PUBLICATION_STATE_TABLE)}"
    provenance = f"{database}.{quote_id(PROVENANCE_TABLE)}"
    try:
        _ensure_tables(cursor, config.target_db)
        cursor.execute(
            f"INSERT IGNORE INTO {state} "
            "(name, mart_publication_epoch, category, epoch, run_id, updated_at) "
            "VALUES (%s, 0, '', '', '', %s)",
            (PUBLICATION_STATE_NAME, now.strftime("%Y-%m-%d %H:%M:%S")),
        )
        conn.commit()
        cursor.execute("START TRANSACTION")
        cursor.execute(
            f"SELECT mart_publication_epoch FROM {state} WHERE name=%s FOR UPDATE",
            (PUBLICATION_STATE_NAME,),
        )
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError("publication state row was not materialized")
        publication_epoch = _first_value(row) + 1
        cursor.execute(
            f"UPDATE {state} SET mart_publication_epoch=%s, category=%s, epoch=%s, "
            "run_id=%s, updated_at=%s WHERE name=%s",
            (
                publication_epoch,
                "iqvia_nsa",
                epoch,
                run_id,
                now.strftime("%Y-%m-%d %H:%M:%S"),
                PUBLICATION_STATE_NAME,
            ),
        )
        cursor.execute(
            f"INSERT INTO {provenance} "
            "(mart_publication_epoch, category, epoch, run_id, "
            "input_inventory_sha256, input_inventory_json, builder_commit, "
            "image_digest, window_start, window_end, published_at_utc, "
            "published_at_kst, status) VALUES "
            "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'published')",
            (
                publication_epoch,
                "iqvia_nsa",
                epoch,
                run_id,
                evidence.inventory_sha256,
                evidence.inventory_json,
                config.builder_commit,
                config.image_ref,
                evidence.window_start,
                evidence.window_end,
                now.isoformat(),
                now.astimezone(KST).isoformat(),
            ),
        )
        conn.commit()
        return publication_epoch
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()


def _mark_publication_rolled_back(
    conn: Any,
    config: PublicationConfig,
    *,
    run_id: str,
    provenance_recorded: bool,
    component_recorded: bool,
) -> None:
    if provenance_recorded:
        cursor = conn.cursor()
        try:
            cursor.execute(
                f"UPDATE {quote_id(config.target_db)}.{quote_id(PROVENANCE_TABLE)} "
                "SET status='rolled_back', rolled_back_at_utc=%s WHERE run_id=%s",
                (datetime.now(timezone.utc).isoformat(), run_id),
            )
            conn.commit()
        finally:
            cursor.close()
    if component_recorded:
        PromotionLedger(conn, dialect="mysql").record_rollback(
            run_id,
            actor="iqvia_nsa_mart_activation",
            reason="publication restored before ingest completion",
        )


def rollback_publication(
    conn: Any,
    config: PublicationConfig,
    *,
    actions: tuple[PublishAction, ...],
    run_id: str,
    restore_run_id: str | None = None,
    provenance_recorded: bool = True,
    component_recorded: bool = True,
) -> None:
    restore_table_group_atomically(
        conn,
        target_db=config.target_db,
        actions=actions,
        run_id=restore_run_id or run_id,
    )
    _mark_publication_rolled_back(
        conn,
        config,
        run_id=run_id,
        provenance_recorded=provenance_recorded,
        component_recorded=component_recorded,
    )
