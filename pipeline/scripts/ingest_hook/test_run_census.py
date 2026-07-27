"""Before/after census for a disposable UBIST test load."""
from __future__ import annotations

import json
import os
from collections import defaultdict
from typing import Any

import pymysql


def _json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value in (None, ""):
        return None
    return json.loads(value)


def _latest_ranks(value: Any) -> dict[str, int]:
    payload = _json(value)
    latest: list[dict] = []
    if isinstance(payload, dict):
        period_rows = [
            (str(period), rows)
            for period, rows in payload.items()
            if isinstance(rows, list)
        ]
        if period_rows:
            latest = [
                row
                for row in max(period_rows, key=lambda item: item[0])[1]
                if isinstance(row, dict)
            ]
    elif isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                continue
            rows = item.get("rankings")
            if isinstance(rows, list):
                latest = [row for row in rows if isinstance(row, dict)]
    return {
        str(row.get("brand") or row.get("brand_key") or row.get("name")): int(row["rank"])
        for row in latest
        if row.get("rank") is not None
        and (row.get("brand") or row.get("brand_key") or row.get("name"))
    }


def _connect(database: str, environ: dict[str, str]):
    return pymysql.connect(
        host=environ.get("MARIADB_HOST", "127.0.0.1"),
        port=int(environ.get("MARIADB_PORT", "3306")),
        user=environ.get("MARIADB_USER", "root"),
        password=environ.get("MARIADB_PASSWORD", ""),
        database=database,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )


def _market_rows(conn) -> dict[tuple[str, str, str], dict]:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT atc4_code, source, measure, market_size_series, brand_ranking
            FROM mart_general_market_metric
            WHERE source = 'ubist'
            """
        )
        return {
            (row["atc4_code"], row["source"], row["measure"]): row
            for row in cursor.fetchall()
        }


def _members(conn) -> dict[tuple[str, str, str], set[str]]:
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT atc4_code, source, measure, brand_key
            FROM mart_general_brand_metric
            WHERE source = 'ubist'
            """
        )
        grouped: dict[tuple[str, str, str], set[str]] = defaultdict(set)
        for row in cursor.fetchall():
            grouped[(row["atc4_code"], row["source"], row["measure"])].add(
                row["brand_key"]
            )
        return dict(grouped)


def build_change_census(environ: dict[str, str] | None = None) -> dict:
    env = environ or os.environ
    source_db = env["MARIADB_SOURCE_DATABASE"]
    target_db = env["INGEST_SHADOW_TARGET_DB"]
    source = _connect(source_db, env)
    target = _connect(target_db, env)
    try:
        before_markets = _market_rows(source)
        after_markets = _market_rows(target)
        before_members = _members(source)
        after_members = _members(target)
    finally:
        source.close()
        target.close()

    keys = sorted(set(before_markets) | set(after_markets))
    changed = []
    denominator_changes = []
    rank_changes = []
    top3_changes = []
    member_details = []
    member_classes = {"added_only": 0, "removed_only": 0, "mixed": 0}
    for key in keys:
        before = before_markets.get(key)
        after = after_markets.get(key)
        if before != after:
            changed.append("|".join(key))
        if _json((before or {}).get("market_size_series")) != _json(
            (after or {}).get("market_size_series")
        ):
            denominator_changes.append("|".join(key))
        before_ranks = _latest_ranks((before or {}).get("brand_ranking"))
        after_ranks = _latest_ranks((after or {}).get("brand_ranking"))
        for brand in sorted(set(before_ranks) | set(after_ranks)):
            old = before_ranks.get(brand)
            new = after_ranks.get(brand)
            if old != new:
                item = {
                    "market": "|".join(key),
                    "brand": brand,
                    "before": old,
                    "after": new,
                }
                rank_changes.append(item)
                if old in {1, 2, 3} or new in {1, 2, 3}:
                    top3_changes.append(item)
        added = sorted(after_members.get(key, set()) - before_members.get(key, set()))
        removed = sorted(before_members.get(key, set()) - after_members.get(key, set()))
        if added or removed:
            classification = (
                "mixed" if added and removed else "added_only" if added else "removed_only"
            )
            member_classes[classification] += 1
            member_details.append(
                {
                    "market": "|".join(key),
                    "classification": classification,
                    "added": added,
                    "removed": removed,
                }
            )
    return {
        "market_population_before": len(before_markets),
        "market_population_after": len(after_markets),
        "changed_markets": len(changed),
        "changed_market_ids": changed,
        "member_changes": member_classes,
        "member_change_details": member_details,
        "rank_changes": rank_changes,
        "top3_rank_changes": top3_changes,
        "denominator_changes": denominator_changes,
    }
