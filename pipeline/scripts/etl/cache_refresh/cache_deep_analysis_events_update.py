from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import pymysql

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
for candidate in (Path("/app"), Path("/workspace")):
    etl_dir = candidate / "pipeline" / "scripts" / "etl"
    if etl_dir.exists():
        for path in (candidate, etl_dir):
            if str(path) not in sys.path:
                sys.path.insert(0, str(path))
        break

from cache_build_common import decode_json, dump_payload, payload_size
from pipeline.mart_config import DEFAULT_MART_DB_NAME
from pipeline.scripts.etl.build_cache_deep_analysis import _rebuild_events_payload_for_brand


TARGET_DATABASE: Final[str] = DEFAULT_MART_DB_NAME


class CacheEventsUpdateError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TableSummary:
    table: str
    rows: int
    payload_hash: str

    def to_json(self) -> dict[str, str | int]:
        return {"table": self.table, "rows": self.rows, "payload_hash": self.payload_hash}


def quote_ident(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", name or ""):
        raise CacheEventsUpdateError(f"unsafe table name: {name!r}")
    return "`" + name.replace("`", "``") + "`"


def stable_json_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def connect_db() -> Any:
    database = os.environ.get("MARIADB_DATABASE", TARGET_DATABASE)
    if database != TARGET_DATABASE:
        raise CacheEventsUpdateError(f"refusing to write non-d2 database: {database}")
    user = os.environ.get("D2_WRITER_USER") or os.environ.get("MARIADB_USER")
    password = os.environ.get("D2_WRITER_PASSWORD") or os.environ.get("MARIADB_PASSWORD")
    if not user or not password:
        raise CacheEventsUpdateError("D2 writer credentials are required")
    return pymysql.connect(
        host=os.environ["MARIADB_HOST"],
        port=int(os.environ.get("MARIADB_PORT", "3306")),
        user=user,
        password=password,
        database=database,
        charset="utf8mb4",
        autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
    )


def table_exists(conn: Any, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) AS c
            FROM information_schema.tables
            WHERE table_schema = DATABASE() AND table_name = %s
            """,
            (table,),
        )
        return int(cur.fetchone()["c"]) == 1


def require_tables(conn: Any, *tables: str) -> None:
    missing = [table for table in tables if not table_exists(conn, table)]
    if missing:
        raise CacheEventsUpdateError("missing required table(s): " + ", ".join(missing))


def table_summary(conn: Any, table: str) -> TableSummary:
    digest = hashlib.sha256()
    rows = 0
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT brand, market_id, response_json, payload_size
            FROM {quote_ident(table)}
            ORDER BY brand, market_id
            """
        )
        for row in cur.fetchall():
            rows += 1
            digest.update(str(row["brand"]).encode())
            digest.update(b"\0")
            digest.update(str(row["market_id"]).encode())
            digest.update(b"\0")
            digest.update(str(row.get("payload_size")).encode())
            digest.update(b"\0")
            digest.update((row.get("response_json") or "").encode())
            digest.update(b"\n")
    return TableSummary(table=table, rows=rows, payload_hash=digest.hexdigest())


def strip_event_fields_from_raw(raw: str | None) -> dict[str, Any]:
    payload = decode_json(raw) or {}
    data = payload.get("data")
    if isinstance(data, dict):
        data = dict(data)
        data.pop("events", None)
        data.pop("events_meta", None)
        payload = dict(payload)
        payload["data"] = data
    return payload


def get_events(payload: Mapping[str, Any]) -> list[Any]:
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return []
    events = data.get("events")
    return events if isinstance(events, list) else []


def max_event_date_for_brand(conn: Any, table: str, brand: str) -> str | None:
    max_date: str | None = None
    with conn.cursor() as cur:
        cur.execute(f"SELECT response_json FROM {quote_ident(table)} WHERE brand = %s", (brand,))
        for row in cur.fetchall():
            payload = decode_json(row.get("response_json")) or {}
            for event in get_events(payload):
                if isinstance(event, Mapping):
                    date = event.get("date")
                    if isinstance(date, str) and (max_date is None or date > max_date):
                        max_date = date
    return max_date


def build_staging(conn: Any, live_table: str, staging_table: str) -> dict[str, Any]:
    require_tables(conn, live_table, "events_raw")
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {quote_ident(staging_table)}")
        cur.execute(f"CREATE TABLE {quote_ident(staging_table)} LIKE {quote_ident(live_table)}")
        cur.execute(f"SELECT brand, market_id, response_json FROM {quote_ident(live_table)} ORDER BY brand, market_id")
        source_rows = list(cur.fetchall())

    columns = ("brand", "market_id", "response_json", "payload_size")
    sql = (
        f"INSERT INTO {quote_ident(staging_table)} "
        f"({', '.join(quote_ident(column) for column in columns)}) VALUES (%s, %s, %s, %s)"
    )
    batch: list[tuple[Any, ...]] = []
    rebuilt_rows = 0
    preserved_rows = 0
    total_events = 0
    rows_with_events = 0

    with conn.cursor() as cur:
        for row in source_rows:
            brand = str(row["brand"])
            payload = decode_json(row.get("response_json")) or {}
            data = payload.setdefault("data", {})
            events, events_meta = _rebuild_events_payload_for_brand(conn, brand)
            data["events"] = events
            data["events_meta"] = events_meta
            rebuilt_rows += 1
            events = get_events(payload)
            if events:
                rows_with_events += 1
                total_events += len(events)
            encoded = dump_payload(payload)
            batch.append((brand, row.get("market_id"), encoded, payload_size(payload)))
            if len(batch) >= 50:
                cur.executemany(sql, batch)
                conn.commit()
                batch = []
        if batch:
            cur.executemany(sql, batch)
            conn.commit()

    return {
        "live_table": live_table,
        "staging_table": staging_table,
        "source_rows": len(source_rows),
        "rebuilt_rows": rebuilt_rows,
        "preserved_rows": preserved_rows,
        "rows_with_events": rows_with_events,
        "total_events": total_events,
        "aktemra_max_event_date": max_event_date_for_brand(conn, staging_table, "악템라"),
    }


def backup_live(conn: Any, live_table: str, backup_table: str) -> dict[str, Any]:
    require_tables(conn, live_table)
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {quote_ident(backup_table)}")
        cur.execute(f"CREATE TABLE {quote_ident(backup_table)} LIKE {quote_ident(live_table)}")
        cur.execute(f"INSERT INTO {quote_ident(backup_table)} SELECT * FROM {quote_ident(live_table)}")
        conn.commit()
    return {"backup": table_summary(conn, backup_table).to_json()}


def assert_same_keyset(conn: Any, left_table: str, right_table: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT COUNT(*) AS c
            FROM {quote_ident(left_table)} l
            LEFT JOIN {quote_ident(right_table)} r USING (brand, market_id)
            WHERE r.brand IS NULL
            """
        )
        left_missing = int(cur.fetchone()["c"])
        cur.execute(
            f"""
            SELECT COUNT(*) AS c
            FROM {quote_ident(right_table)} r
            LEFT JOIN {quote_ident(left_table)} l USING (brand, market_id)
            WHERE l.brand IS NULL
            """
        )
        right_missing = int(cur.fetchone()["c"])
    if left_missing or right_missing:
        raise CacheEventsUpdateError(
            f"table keyset mismatch: {left_table}-only={left_missing}, {right_table}-only={right_missing}"
        )


def apply_events_update(conn: Any, live_table: str, staging_table: str) -> dict[str, Any]:
    require_tables(conn, live_table, staging_table)
    assert_same_keyset(conn, live_table, staging_table)
    before = table_summary(conn, live_table)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {quote_ident(live_table)} live
            JOIN {quote_ident(staging_table)} staging USING (brand, market_id)
            SET live.response_json = staging.response_json,
                live.payload_size = staging.payload_size
            """
        )
        updated = cur.rowcount
        conn.commit()
    after = table_summary(conn, live_table)
    return {"before": before.to_json(), "after": after.to_json(), "updated_rows": updated}


def verify_after_update(conn: Any, live_table: str, backup_table: str) -> dict[str, Any]:
    require_tables(conn, live_table, backup_table)
    assert_same_keyset(conn, live_table, backup_table)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT live.brand, live.market_id, live.response_json AS live_json, backup.response_json AS backup_json
            FROM {quote_ident(live_table)} live
            JOIN {quote_ident(backup_table)} backup USING (brand, market_id)
            ORDER BY live.brand, live.market_id
            """
        )
        rows = list(cur.fetchall())

    non_events_diff = 0
    events_changed = 0
    rows_with_events = 0
    total_events = 0
    for row in rows:
        live_raw = row.get("live_json")
        backup_raw = row.get("backup_json")
        live_payload = decode_json(live_raw) or {}
        backup_payload = decode_json(backup_raw) or {}
        if stable_json_hash(strip_event_fields_from_raw(live_raw)) != stable_json_hash(
            strip_event_fields_from_raw(backup_raw)
        ):
            non_events_diff += 1
        if stable_json_hash(get_events(live_payload)) != stable_json_hash(get_events(backup_payload)):
            events_changed += 1
        events = get_events(live_payload)
        if events:
            rows_with_events += 1
            total_events += len(events)
    if non_events_diff:
        raise CacheEventsUpdateError(f"non-events changed after update: {non_events_diff}")
    return {
        "rows_checked": len(rows),
        "non_events_diff_count": non_events_diff,
        "events_changed_rows": events_changed,
        "rows_with_events": rows_with_events,
        "total_events": total_events,
        "aktemra_max_event_date": max_event_date_for_brand(conn, live_table, "악템라"),
    }


def drop_table(conn: Any, table: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {quote_ident(table)}")
        conn.commit()
    return {"dropped_table": table, "exists_after_drop": table_exists(conn, table)}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update d2 cache_deep_analysis events with full row coverage preserved.")
    parser.add_argument("--live-table", default=os.environ.get("LIVE_TABLE", "cache_deep_analysis"))
    parser.add_argument("--staging-table", default=os.environ.get("STAGING_TABLE"))
    parser.add_argument("--backup-table", default=os.environ.get("BACKUP_TABLE"))
    parser.add_argument("--build-staging", action="store_true")
    parser.add_argument("--backup-live", action="store_true")
    parser.add_argument("--apply-update", action="store_true")
    parser.add_argument("--post-verify", action="store_true")
    parser.add_argument("--drop-staging", action="store_true")
    args = parser.parse_args()
    if (args.build_staging or args.apply_update or args.drop_staging) and not args.staging_table:
        raise CacheEventsUpdateError("--staging-table or STAGING_TABLE is required")
    if (args.backup_live or args.post_verify) and not args.backup_table:
        raise CacheEventsUpdateError("--backup-table or BACKUP_TABLE is required")
    if not any((args.build_staging, args.backup_live, args.apply_update, args.post_verify, args.drop_staging)):
        raise CacheEventsUpdateError("at least one action is required")
    return args


def main() -> None:
    args = parse_args()
    conn = connect_db()
    summary: dict[str, Any] = {"database": os.environ.get("MARIADB_DATABASE", TARGET_DATABASE)}
    try:
        if args.build_staging:
            summary["build_staging"] = build_staging(conn, args.live_table, args.staging_table)
            summary["staging"] = table_summary(conn, args.staging_table).to_json()
        if args.backup_live:
            summary["backup_live"] = backup_live(conn, args.live_table, args.backup_table)
        if args.apply_update:
            summary["apply_update"] = apply_events_update(conn, args.live_table, args.staging_table)
        if args.post_verify:
            summary["post_verify"] = verify_after_update(conn, args.live_table, args.backup_table)
        if args.drop_staging:
            summary["drop_staging"] = drop_table(conn, args.staging_table)
        print("CACHE_EVENTS_UPDATE_JSON=" + json.dumps(summary, ensure_ascii=False, default=str))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
