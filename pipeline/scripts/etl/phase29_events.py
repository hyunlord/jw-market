#!/usr/bin/env python3
"""Phase 29 Agent 1 event sync and Cut A/B helpers.

The Agent 1 tables already exist in the local jw_mart mirror as `news_raw`
and `event_brand_scores`. Phase 29 adds a stable `events_raw` clone for the
deep-analysis cache and reads scores without recalculating or modifying them.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import date, datetime
from difflib import SequenceMatcher
from pathlib import Path
import sys
from typing import Any

import pymysql

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.etl.io.mart.event_score_policy import (
    event_score_policy,
    is_cut_b_exposed,
    is_news_exposed,
)

try:
    from pipeline.scripts.etl.cache_build_common import mariadb_connect
except ModuleNotFoundError:  # pragma: no cover - script execution from etl dir
    from cache_build_common import mariadb_connect


TAG_TO_CATEGORY = {
    "신약/R&D": "rd",
    "정책/규제": "policy",
    "공급/생산": "supply",
    "자본/경영": "capital",
    "외부/트렌드": "external",
    "기타": "external",
}


def connect() -> pymysql.connections.Connection:
    return mariadb_connect()


def ensure_events_raw_table(conn: pymysql.connections.Connection) -> None:
    """Create/sync `events_raw` from Agent 1 `news_raw` without touching scores."""
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS events_raw (
                news_id VARCHAR(64) PRIMARY KEY,
                source_name VARCHAR(100),
                published_date DATE,
                title TEXT,
                summary TEXT,
                body LONGTEXT,
                url TEXT,
                created_at DATETIME,
                ingested_at DATETIME,
                INDEX idx_events_raw_published (published_date),
                INDEX idx_events_raw_source (source_name)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_uca1400_ai_ci
            """
        )
        cur.execute("ALTER TABLE events_raw CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_uca1400_ai_ci")
        cur.execute(
            """
            INSERT INTO events_raw (
                news_id, source_name, published_date, title, summary, body, url, created_at, ingested_at
            )
            SELECT
                news_id,
                source_name,
                published_date,
                title,
                LEFT(COALESCE(article_text, ''), 1000) AS summary,
                article_text AS body,
                article_url AS url,
                ingested_at AS created_at,
                ingested_at
            FROM news_raw
            ON DUPLICATE KEY UPDATE
                source_name = VALUES(source_name),
                published_date = VALUES(published_date),
                title = VALUES(title),
                summary = VALUES(summary),
                body = VALUES(body),
                url = VALUES(url),
                created_at = VALUES(created_at),
                ingested_at = VALUES(ingested_at)
            """
        )
        _ensure_index(cur, "event_brand_scores", "idx_phase29_brand_score", ["brand_canonical", "score"])
        _ensure_index(cur, "event_brand_scores", "idx_phase29_news", ["news_id"])


def _ensure_index(cur: Any, table: str, index_name: str, columns: list[str]) -> None:
    existing_columns = _table_columns(cur, table)
    if not set(columns).issubset(existing_columns):
        return
    cur.execute(
        """
        SELECT COUNT(*) AS cnt
        FROM information_schema.statistics
        WHERE table_schema = DATABASE()
          AND table_name = %s
          AND index_name = %s
        """,
        [table, index_name],
    )
    if int(cur.fetchone()["cnt"]) > 0:
        return
    cols = ", ".join(f"`{column}`" for column in columns)
    cur.execute(f"CREATE INDEX `{index_name}` ON `{table}` ({cols})")


def _table_columns(cur: Any, table: str) -> set[str]:
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = %s
        """,
        [table],
    )
    return {str(row["column_name"]) for row in cur.fetchall()}


def period_map_for_date(value: Any) -> dict[str, str | None]:
    if isinstance(value, datetime):
        d = value.date()
    elif isinstance(value, date):
        d = value
    else:
        try:
            d = datetime.fromisoformat(str(value)[:10]).date()
        except ValueError:
            return {"UBIST": None, "IQVIA": None}
    quarter = ((d.month - 1) // 3) + 1
    return {"UBIST": f"{d.year:04d}-{d.month:02d}", "IQVIA": f"{d.year:04d}-Q{quarter}"}


def _decode_json(value: Any) -> Any:
    if not value:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def format_event(row: dict[str, Any], *, cut_threshold: int) -> dict[str, Any]:
    event_date = row.get("published_date")
    summary = row.get("event_summary") or row.get("summary") or ""
    event_id = row.get("event_id") or row.get("news_id")
    score = int(row.get("score") or 0)
    tag = row.get("tag") or "기타"
    return {
        "id": str(event_id),
        "event_id": str(event_id),
        "news_id": str(row.get("news_id")),
        "brand": row.get("brand_canonical") or row.get("brand_name"),
        "brand_name": row.get("brand_name"),
        "score": score,
        "impact_score": score,
        "cut_threshold": cut_threshold,
        "tag": tag,
        "category": TAG_TO_CATEGORY.get(str(tag), "external"),
        "derivation": row.get("derivation"),
        "title": row.get("title") or "",
        "summary": summary,
        "body_full": row.get("body") or summary,
        "source": row.get("source_name"),
        "url": row.get("news_url"),
        "source_url": row.get("event_source_url") or row.get("news_url"),
        "date": str(event_date) if event_date is not None else None,
        "published_date": str(event_date) if event_date is not None else None,
        "reason": row.get("reason"),
        "mirrored_from_jw_brands": _decode_json(row.get("mirrored_from_jw_brands")),
        "period_map": period_map_for_date(event_date),
    }


def _normalize_title(title: str | None) -> str:
    text = (title or "").lower()
    text = re.sub(r"[^\w\s가-힣]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _cut_a_unique_cluster_count(events: list[dict[str, Any]], similarity_threshold: float = 0.80) -> int:
    clusters_by_key: dict[tuple[Any, Any, Any], list[list[dict[str, Any]]]] = {}
    for event in events:
        key = (
            event.get("brand_name") or event.get("brand"),
            event.get("date") or event.get("published_date"),
            event.get("category"),
        )
        clusters = clusters_by_key.setdefault(key, [])
        title_norm = _normalize_title(event.get("title"))
        matched = False
        for cluster in clusters:
            if title_norm and any(
                SequenceMatcher(None, title_norm, _normalize_title(clustered.get("title"))).ratio() >= similarity_threshold
                for clustered in cluster
                if _normalize_title(clustered.get("title"))
            ):
                cluster.append(event)
                matched = True
                break
        if not matched:
            clusters.append([event])
    return sum(len(clusters) for clusters in clusters_by_key.values())


def _query_events(
    conn: pymysql.connections.Connection,
    brand: str,
    *,
    min_score: int,
    lookback_months: int | None,
    limit: int | None,
    derivation: str | None = None,
) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS cnt FROM event_brand_scores")
        if int(cur.fetchone()["cnt"] or 0) == 0:
            return []

    where = [
        "COALESCE(s.brand_canonical, s.brand_name) = %s",
        "s.score >= %s",
    ]
    params: list[Any] = [brand, min_score]
    if derivation:
        where.append("s.derivation = %s")
        params.append(derivation)
    if lookback_months is not None:
        where.append("n.published_date >= DATE_SUB(CURRENT_DATE, INTERVAL %s MONTH)")
        params.append(lookback_months)
    limit_sql = ""
    if limit is not None:
        limit_sql = "LIMIT %s"
        params.append(limit)
    sql = f"""
        SELECT
            s.event_id,
            s.news_id,
            s.brand_name,
            s.brand_canonical,
            s.score,
            s.tag,
            s.source_processor,
            s.derivation,
            s.reason,
            s.mirrored_from_jw_brands,
            s.summary AS event_summary,
            n.title,
            n.summary,
            n.body,
            n.source_name,
            n.published_date,
            n.url AS news_url,
            e.source_url AS event_source_url
        FROM event_brand_scores s
        JOIN events_raw n ON s.news_id = n.news_id
        LEFT JOIN events e ON s.event_id = e.event_id
        WHERE {" AND ".join(where)}
        ORDER BY s.score DESC, n.published_date DESC, s.id DESC
        {limit_sql}
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def get_brand_events_cut_a(
    conn: pymysql.connections.Connection,
    brand: str,
    *,
    target_min: int = 5,
    target_max: int = 50,
    lookback_candidates: list[int | None] | None = None,
) -> tuple[list[dict[str, Any]], int | None, int | None]:
    """Cut A: expand lookback and lower threshold until target coverage exists."""
    if lookback_candidates is None:
        lookback_candidates = [6, 12, 24, None]

    formatted: list[dict[str, Any]] = []
    final_lookback: int | None = None
    final_threshold: int | None = None

    for lookback_months in lookback_candidates:
        threshold = 50
        while threshold >= 0:
            rows = _query_events(conn, brand, min_score=threshold, lookback_months=lookback_months, limit=None)
            exposed_rows = _filter_news_exposure_rows(rows)[:target_max]
            formatted = [
                format_event(row, cut_threshold=max(threshold, _news_cutoff(row)))
                for row in exposed_rows
            ]
            if (len(formatted) >= target_min and _cut_a_unique_cluster_count(formatted) >= target_min) or threshold == 0:
                break
            threshold -= 1

        final_lookback = lookback_months
        final_threshold = threshold
        if len(formatted) >= target_min and _cut_a_unique_cluster_count(formatted) >= target_min:
            break

    return formatted, final_lookback, final_threshold


def get_brand_events_cut_b(
    conn: pymysql.connections.Connection,
    brand: str,
    *,
    lookback_months: int | None = 6,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Cut B: processor-versioned direct-only chart/model events."""
    rows = _query_events(
        conn,
        brand,
        min_score=80,
        lookback_months=lookback_months,
        limit=None,
        derivation="llm_direct",
    )
    exposed_rows = _filter_cut_b_rows(rows)
    if limit is not None:
        exposed_rows = exposed_rows[:limit]
    return [
        format_event(
            row,
            cut_threshold=event_score_policy(row.get("source_processor")).cut_b_threshold,
        )
        for row in exposed_rows
    ]


def _filter_news_exposure_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if is_news_exposed(
            tag=row.get("tag"),
            score=int(row.get("score") or 0),
            source_processor=row.get("source_processor"),
        )
    ]


def _filter_cut_b_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        row
        for row in _filter_news_exposure_rows(rows)
        if is_cut_b_exposed(
            score=int(row.get("score") or 0),
            source_processor=row.get("source_processor"),
        )
    ]


def _news_cutoff(row: dict[str, Any]) -> int:
    policy = event_score_policy(row.get("source_processor"))
    return policy.category_cutoffs[str(row.get("tag"))]


def build_events_for_cache(conn: pymysql.connections.Connection, brand: str) -> dict[str, Any]:
    cut_a, cut_a_final_lookback, cut_a_final_threshold = get_brand_events_cut_a(conn, brand)
    cut_b = get_brand_events_cut_b(conn, brand)
    return {
        "cut_a": cut_a,
        "cut_b": cut_b,
        "meta": {
            "lookback_months": 6,
            "cut_a_target_min": 5,
            "cut_a_target_max": 50,
            "cut_a_threshold": cut_a_final_threshold,
            "cut_a_final_lookback_months": cut_a_final_lookback,
            "cut_b_threshold": 80,
            "cut_b_threshold_rev5674": 88,
            "cut_b_derivation": "llm_direct",
        },
    }


def table_counts(conn: pymysql.connections.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    with conn.cursor() as cur:
        for table in ("news_raw", "events_raw", "event_brand_scores"):
            cur.execute(f"SELECT COUNT(*) AS cnt FROM `{table}`")
            counts[table] = int(cur.fetchone()["cnt"])
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-out")
    args = parser.parse_args()
    conn = connect()
    try:
        ensure_events_raw_table(conn)
        counts = table_counts(conn)
    finally:
        conn.close()
    report = {"phase": "29", "loader": "events_raw_sync", "counts": counts}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
