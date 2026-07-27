"""Post-publication epoch and downstream propagation plan.

This module owns only the signal emitted after the serving mart has already
been atomically published. It does not rebuild or mutate caches directly; it
records a monotonic mart publication epoch and returns a declarative plan for
the normal cache layer and notification consumers.
"""
from __future__ import annotations

import os
import re
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final, Protocol


NORMAL_CACHE_TABLES = ("cache_brands", "cache_market_status")
PUBLICATION_STATE_NAME = "normal_caches"
ENV_PUBLICATION_EPOCH_TABLE: Final = "INGEST_PUBLICATION_EPOCH_TABLE"
DEFAULT_PUBLICATION_EPOCH_TABLE: Final = "ingest_publication_state"
ENV_PUBLICATION_PROVENANCE_TABLE: Final = "INGEST_PUBLICATION_PROVENANCE_TABLE"
DEFAULT_PUBLICATION_PROVENANCE_TABLE: Final = "mart_publication_provenance"
_SQL_IDENTIFIER: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_FULL_GIT_SHA: Final = re.compile(r"^[0-9a-f]{40}$")
_IMMUTABLE_IMAGE: Final = re.compile(r"^.+@sha256:[0-9a-f]{64}$")
KST: Final = timezone(timedelta(hours=9))


class Cursor(Protocol):
    def execute(self, statement: str, parameters: tuple = ()) -> None: ...

    def fetchone(self) -> tuple | dict | None: ...


class Connection(Protocol):
    def cursor(self) -> Cursor: ...

    def commit(self) -> object: ...

    def rollback(self) -> object: ...


class ConnectionFactory(Protocol):
    def __call__(self) -> Connection: ...


@dataclass(frozen=True, slots=True)
class CacheInvalidationPlan:
    tables: tuple[str, ...]
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {"tables": list(self.tables), "reason": self.reason}


@dataclass(frozen=True, slots=True)
class PublicationResult:
    category: str
    epoch: str
    run_id: str
    status: str
    mart_publication_epoch: int | None
    cache_invalidation: CacheInvalidationPlan
    dashboard_payload: dict[str, object]
    chat_payload: dict[str, object]
    reason: str | None = None

    def as_status_payload(self) -> dict[str, object]:
        return {
            "stage": "publication_signal",
            "status": self.status,
            "reason": self.reason,
            "mart_publication_epoch": self.mart_publication_epoch,
            "cache_invalidation": self.cache_invalidation.as_dict(),
            "notifications": {
                "dashboard": self.dashboard_payload,
                "chat": self.chat_payload,
            },
        }


@dataclass(frozen=True, slots=True)
class PublicationProvenance:
    inventory_sha256: str
    inventory_json: str
    builder_commit: str
    image_digest: str
    window_start: str
    window_end: str
    published_at_utc: str
    published_at_kst: str


def _resolve_builder_commit(builder_commit: str | None) -> str:
    image_commit = os.environ.get("APP_VERSION", "").strip().lower()
    legacy_candidates = (
        ("builder_commit", builder_commit),
        ("BUILD_GIT_SHA", os.environ.get("BUILD_GIT_SHA")),
        ("R1_GIT_COMMIT", os.environ.get("R1_GIT_COMMIT")),
    )

    if image_commit:
        if not _FULL_GIT_SHA.fullmatch(image_commit):
            raise ValueError("image APP_VERSION must be a full git commit SHA")
        for source, candidate in legacy_candidates:
            resolved = (candidate or "").strip().lower()
            if resolved and resolved != image_commit:
                raise ValueError(
                    f"{source} does not match image APP_VERSION"
                )
        return image_commit

    commit = next(
        (
            str(candidate).strip().lower()
            for _, candidate in legacy_candidates
            if candidate
        ),
        "",
    )
    if not _FULL_GIT_SHA.fullmatch(commit):
        raise ValueError(
            "full 40-character builder commit SHA is required for mart publication"
        )
    return commit


def build_provenance(
    files: object,
    *,
    file_rows: dict[str, int],
    periods: set[str] | tuple[str, ...] | list[str],
    builder_commit: str | None = None,
    image_digest: str | None = None,
) -> PublicationProvenance:
    """Build a bounded manifest fingerprint without scanning source contents."""
    canonical_files = sorted(
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
        canonical_files,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    commit = _resolve_builder_commit(builder_commit)
    ordered_periods = sorted(set(periods))
    if not ordered_periods:
        raise ValueError("publication window is empty")
    now = datetime.now(timezone.utc)
    resolved_image = (
        image_digest or os.environ.get("INGEST_JOB_IMAGE") or ""
    ).strip()
    if not _IMMUTABLE_IMAGE.fullmatch(resolved_image):
        raise ValueError(
            "immutable ingest image digest is required for mart publication"
        )
    return PublicationProvenance(
        inventory_sha256=hashlib.sha256(inventory_json.encode("utf-8")).hexdigest(),
        inventory_json=inventory_json,
        builder_commit=commit,
        image_digest=resolved_image,
        window_start=ordered_periods[0],
        window_end=ordered_periods[-1],
        published_at_utc=now.isoformat(),
        published_at_kst=now.astimezone(KST).isoformat(),
    )


def publish_completion(
    category: str,
    epoch: str,
    run_id: str,
    connection_factory: ConnectionFactory | None = None,
    dry_run: bool = False,
    provenance: PublicationProvenance | None = None,
) -> PublicationResult:
    """Record mart publication and return cache/notification propagation plans."""

    cache_plan = CacheInvalidationPlan(
        tables=NORMAL_CACHE_TABLES,
        reason="serving mart publication completed",
    )
    if dry_run:
        publication_epoch = None
        status = "planned"
    else:
        if connection_factory is None:
            from pipeline.scripts.ingest_hook import config

            connection_factory = config.open_mart_connection
        publication_epoch = _record_publication_epoch(
            connection_factory(),
            category=category,
            epoch=epoch,
            run_id=run_id,
            provenance=provenance,
        )
        status = "recorded"
    dashboard_payload = _notification_payload(
        event=f"mart_publication_{status}",
        channel="dashboard",
        category=category,
        epoch=epoch,
        run_id=run_id,
        mart_publication_epoch=publication_epoch,
        cache_plan=cache_plan,
    )
    chat_payload = _notification_payload(
        event=f"mart_publication_{status}",
        channel="chat",
        category=category,
        epoch=epoch,
        run_id=run_id,
        mart_publication_epoch=publication_epoch,
        cache_plan=cache_plan,
    )
    return PublicationResult(
        category=category,
        epoch=epoch,
        run_id=run_id,
        status=status,
        mart_publication_epoch=publication_epoch,
        cache_invalidation=cache_plan,
        dashboard_payload=dashboard_payload,
        chat_payload=chat_payload,
    )


def _record_publication_epoch(
    conn: Connection,
    *,
    category: str,
    epoch: str,
    run_id: str,
    provenance: PublicationProvenance | None,
) -> int:
    mark = _parameter_marker(conn)
    table = _publication_table()
    quoted_table = f"`{table}`"
    cursor = conn.cursor()
    updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {quoted_table} (
          name VARCHAR(64) PRIMARY KEY,
          mart_publication_epoch BIGINT NOT NULL,
          category VARCHAR(32) NOT NULL,
          epoch VARCHAR(32) NOT NULL,
          run_id VARCHAR(64) NOT NULL,
          updated_at VARCHAR(32) NOT NULL
        )
        """
    )
    _ensure_publication_state(
        cursor,
        table=quoted_table,
        mark=mark,
        updated_at=updated_at,
    )
    if provenance is not None:
        _ensure_provenance_table(cursor)
    conn.commit()
    try:
        _begin_transaction(cursor, conn)
        select_sql = (
            f"SELECT mart_publication_epoch FROM {quoted_table}"
            f" WHERE name={mark}"
        )
        if mark == "%s":
            select_sql += " FOR UPDATE"
        cursor.execute(select_sql, (PUBLICATION_STATE_NAME,))
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError("publication state row was not materialized")
        existing_epoch = _existing_provenance_epoch(
            cursor,
            mark=mark,
            category=category,
            epoch=epoch,
            provenance=provenance,
        )
        if existing_epoch is not None:
            conn.commit()
            return existing_epoch
        next_epoch = _first_int(row) + 1
        updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute(
            f"UPDATE {quoted_table}"
            f" SET mart_publication_epoch={mark}, category={mark}, epoch={mark},"
            f" run_id={mark}, updated_at={mark} WHERE name={mark}",
            (
                next_epoch,
                category,
                epoch,
                run_id,
                updated_at,
                PUBLICATION_STATE_NAME,
            ),
        )
        if provenance is not None:
            _record_provenance(
                cursor,
                mark=mark,
                publication_epoch=next_epoch,
                category=category,
                epoch=epoch,
                run_id=run_id,
                provenance=provenance,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return next_epoch


def _existing_provenance_epoch(
    cursor: Cursor,
    *,
    mark: str,
    category: str,
    epoch: str,
    provenance: PublicationProvenance | None,
) -> int | None:
    if provenance is None:
        return None
    table = f"`{_provenance_table()}`"
    cursor.execute(
        f"SELECT mart_publication_epoch FROM {table} "
        f"WHERE category={mark} AND epoch={mark} "
        f"AND input_inventory_sha256={mark} AND builder_commit={mark} "
        f"AND image_digest={mark} AND window_start={mark} AND window_end={mark}",
        (
            category,
            epoch,
            provenance.inventory_sha256,
            provenance.builder_commit,
            provenance.image_digest,
            provenance.window_start,
            provenance.window_end,
        ),
    )
    row = cursor.fetchone()
    return None if row is None else _first_int(row)


def _record_provenance(
    cursor: Cursor,
    *,
    mark: str,
    publication_epoch: int,
    category: str,
    epoch: str,
    run_id: str,
    provenance: PublicationProvenance,
) -> None:
    table = f"`{_provenance_table()}`"
    cursor.execute(
        f"INSERT INTO {table} "
        "(mart_publication_epoch, category, epoch, run_id, "
        "input_inventory_sha256, input_inventory_json, builder_commit, "
        "image_digest, window_start, window_end, published_at_utc, "
        f"published_at_kst) VALUES ({', '.join([mark] * 12)})",
        (
            publication_epoch,
            category,
            epoch,
            run_id,
            provenance.inventory_sha256,
            provenance.inventory_json,
            provenance.builder_commit,
            provenance.image_digest,
            provenance.window_start,
            provenance.window_end,
            provenance.published_at_utc,
            provenance.published_at_kst,
        ),
    )


def _ensure_provenance_table(cursor: Cursor) -> None:
    table = f"`{_provenance_table()}`"
    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {table} (
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
          published_at_kst VARCHAR(40) NOT NULL
        )
        """
    )


def _ensure_publication_state(
    cursor: Cursor,
    *,
    table: str,
    mark: str,
    updated_at: str,
) -> None:
    verb = "INSERT OR IGNORE" if mark == "?" else "INSERT IGNORE"
    cursor.execute(
        f"{verb} INTO {table}"
        " (name, mart_publication_epoch, category, epoch, run_id, updated_at)"
        f" VALUES ({', '.join([mark] * 6)})",
        (PUBLICATION_STATE_NAME, 0, "", "", "", updated_at),
    )


def _publication_table() -> str:
    table = os.environ.get(
        ENV_PUBLICATION_EPOCH_TABLE,
        DEFAULT_PUBLICATION_EPOCH_TABLE,
    ).strip()
    if not _SQL_IDENTIFIER.fullmatch(table):
        raise ValueError(
            f"{ENV_PUBLICATION_EPOCH_TABLE} must be a SQL identifier"
        )
    return table


def _provenance_table() -> str:
    table = os.environ.get(
        ENV_PUBLICATION_PROVENANCE_TABLE,
        DEFAULT_PUBLICATION_PROVENANCE_TABLE,
    ).strip()
    if not _SQL_IDENTIFIER.fullmatch(table):
        raise ValueError(
            f"{ENV_PUBLICATION_PROVENANCE_TABLE} must be a SQL identifier"
        )
    return table


def _parameter_marker(conn: Connection) -> str:
    return "?" if conn.__class__.__module__ == "sqlite3" else "%s"


def _begin_transaction(cursor: Cursor, conn: Connection) -> None:
    if conn.__class__.__module__ == "sqlite3":
        cursor.execute("BEGIN IMMEDIATE")
    else:
        cursor.execute("START TRANSACTION")


def _first_int(row: tuple | dict) -> int:
    value = next(iter(row.values())) if isinstance(row, dict) else row[0]
    return int(value)


def _notification_payload(
    *,
    event: str,
    channel: str,
    category: str,
    epoch: str,
    run_id: str,
    mart_publication_epoch: int | None,
    cache_plan: CacheInvalidationPlan,
) -> dict[str, object]:
    return {
        "event": event,
        "channel": channel,
        "category": category,
        "epoch": epoch,
        "run_id": run_id,
        "mart_publication_epoch": mart_publication_epoch,
        "cache_invalidation": cache_plan.as_dict(),
    }
