from __future__ import annotations

import json
from collections import Counter
from typing import Dict

from .config import EventConfig

TAGS = ["신약/R&D", "정책/규제", "공급/생산", "자본/경영", "외부/트렌드", "기타"]


def _clean_text(value):
    return value or ""


def _event_row(row, include_reason=True, include_mirror=False):
    base = {
        "news_id": row["news_id"],
        "published_date": str(row["published_date"]) if row["published_date"] else None,
        "score": int(row["score"]),
        "tag": row.get("tag"),
        "title": _clean_text(row.get("title")),
        "summary": _clean_text(row.get("summary")),
        "source_name": row.get("source_name"),
    }
    if include_reason:
        base["reason"] = _clean_text(row.get("reason"))
    if include_mirror:
        try:
            mirrored = json.loads(row.get("mirrored_from_jw_brands") or "[]")
        except Exception:
            mirrored = []
        base["mirrored_from"] = mirrored
    return base


def build_event_bundle(
    brand: str,
    db_conn,
    snapshot_at,
    config: EventConfig,
) -> Dict:
    snapshot_date = snapshot_at.date().isoformat()
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.news_id, n.published_date, s.score, s.tag, n.title,
                   s.summary, s.reason, n.source_name
            FROM event_brand_scores s
            JOIN news_raw n ON s.news_id = n.news_id
            WHERE (s.brand_canonical = %s OR s.brand_name = %s)
              AND s.derivation = 'llm_direct'
              AND s.source_processor = 'workflow_196_optionB'
              AND s.score >= %s
              AND n.published_date >= DATE_SUB(%s, INTERVAL %s MONTH)
              AND n.published_date <= %s
            ORDER BY s.score DESC, n.published_date DESC, s.news_id ASC
            LIMIT %s
            """,
            (
                brand,
                brand,
                config.min_score_direct,
                snapshot_date,
                config.lookback_months,
                snapshot_date,
                config.max_count_direct,
            ),
        )
        direct_rows = cur.fetchall()

        cur.execute(
            """
            SELECT s.news_id, n.published_date, s.score, s.tag, n.title,
                   s.summary, n.source_name, s.mirrored_from_jw_brands
            FROM event_brand_scores s
            JOIN news_raw n ON s.news_id = n.news_id
            WHERE (s.brand_name = %s OR s.brand_canonical = %s OR s.mirrored_from_jw_brands LIKE %s)
              AND s.derivation = 'cross_match'
              AND s.source_processor = 'cross_match_adapter_v1'
              AND s.score >= %s
              AND n.published_date >= DATE_SUB(%s, INTERVAL %s MONTH)
              AND n.published_date <= %s
            ORDER BY s.score DESC, n.published_date DESC, s.news_id ASC
            LIMIT %s
            """,
            (
                brand,
                brand,
                f'%"{brand}"%',
                config.min_score_cross,
                snapshot_date,
                config.lookback_months,
                snapshot_date,
                config.max_count_cross,
            ),
        )
        cross_rows = cur.fetchall()

    direct_events = [_event_row(row) for row in direct_rows]
    cross_events = [_event_row(row, include_reason=False, include_mirror=True) for row in cross_rows]
    counts = Counter(event["tag"] for event in direct_events)
    tag_distribution = {tag: int(counts.get(tag, 0)) for tag in TAGS}
    return {
        "direct_events": direct_events,
        "cross_match_events": cross_events,
        "tag_distribution": tag_distribution,
    }
