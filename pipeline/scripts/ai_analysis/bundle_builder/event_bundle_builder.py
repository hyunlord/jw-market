from __future__ import annotations

import json
from collections import Counter
from typing import Dict, Optional, Tuple

from .config import EventConfig

TAGS = ["신약/R&D", "정책/규제", "공급/생산", "자본/경영", "외부/트렌드", "기타"]
DIRECT_EVENT_SOURCE_PROCESSORS = (
    "workflow_196_optionB",
    "workflow_196_rev5674",
    "tier2_llm_v1",
)
CROSS_MATCH_SOURCE_PROCESSORS = ("cross_match_adapter_v1",)
# Tier2 processor policy:
# - tier2_llm_v1 is LLM-confirmed brand/article evidence and is visible to Agent2.
# - tier2_exact_rule_v1 is search/exact-rule provenance only and intentionally
#   stays outside Agent2 narrative evidence.


def _sql_placeholders(values) -> str:
    return ",".join(["%s"] * len(values))


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


def _build_event_bundle_v1(
    brand: str,
    db_conn,
    snapshot_at,
    config: EventConfig,
) -> Dict:
    snapshot_date = snapshot_at.date().isoformat()
    with db_conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT s.news_id, n.published_date, s.score, s.tag, n.title,
                   s.summary, s.reason, n.source_name
            FROM event_brand_scores s
            JOIN news_raw n ON s.news_id = n.news_id
            WHERE (s.brand_canonical = %s OR s.brand_name = %s)
              AND s.derivation = 'llm_direct'
              AND s.source_processor IN ({_sql_placeholders(DIRECT_EVENT_SOURCE_PROCESSORS)})
              AND s.tag <> '기타'
              AND s.score >= %s
              AND n.published_date >= DATE_SUB(%s, INTERVAL %s MONTH)
              AND n.published_date <= %s
            ORDER BY s.score DESC, n.published_date DESC, s.news_id ASC
            LIMIT %s
            """,
            (
                brand,
                brand,
                *DIRECT_EVENT_SOURCE_PROCESSORS,
                config.min_score_direct,
                snapshot_date,
                config.lookback_months,
                snapshot_date,
                config.max_count_direct,
            ),
        )
        direct_rows = cur.fetchall()

        cur.execute(
            f"""
            SELECT s.news_id, n.published_date, s.score, s.tag, n.title,
                   s.summary, n.source_name, s.mirrored_from_jw_brands
            FROM event_brand_scores s
            JOIN news_raw n ON s.news_id = n.news_id
            WHERE (s.brand_name = %s OR s.brand_canonical = %s OR s.mirrored_from_jw_brands LIKE %s)
              AND s.derivation = 'cross_match'
              AND s.source_processor IN ({_sql_placeholders(CROSS_MATCH_SOURCE_PROCESSORS)})
              AND s.tag <> '기타'
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
                *CROSS_MATCH_SOURCE_PROCESSORS,
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


def is_brand_centric(
    event: dict,
    brand_korean: str,
    brand_english: Optional[str],
) -> Tuple[bool, str]:
    fields = (("title", event.get("title") or ""), ("summary", event.get("summary") or ""))
    for field, text in fields:
        if brand_korean and brand_korean in text:
            return True, f"{brand_korean} in {field}"
        if brand_english and brand_english.upper() in text.upper():
            return True, f"{brand_english} in {field}"
    return False, ""


def _dedup_by_date(events: list[dict]) -> list[dict]:
    by_date = {}
    for event in events:
        key = event["published_date"]
        existing = by_date.get(key)
        if existing is None or (event["score"], event["news_id"]) > (existing["score"], existing["news_id"]):
            by_date[key] = event
    return sorted(by_date.values(), key=lambda item: (-item["score"], item["published_date"] or "", item["news_id"]))


def _build_event_bundle_v1_1(
    brand_context: dict,
    snapshot_at,
    config,
    db_conn,
) -> Dict:
    event_config = config.event
    brand = brand_context["name"]
    snapshot_date = snapshot_at.date().isoformat()
    with db_conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT s.news_id, n.published_date, s.score, s.tag, n.title,
                   s.summary, s.reason, n.source_name
            FROM event_brand_scores s
            JOIN news_raw n ON s.news_id = n.news_id
            WHERE (s.brand_canonical = %s OR s.brand_name = %s)
              AND s.derivation = 'llm_direct'
              AND s.source_processor IN ({_sql_placeholders(DIRECT_EVENT_SOURCE_PROCESSORS)})
              AND s.tag <> '기타'
              AND s.score >= %s
              AND n.published_date >= DATE_SUB(%s, INTERVAL %s MONTH)
              AND n.published_date <= %s
            ORDER BY s.score DESC, n.published_date DESC, s.news_id ASC
            LIMIT %s
            """,
            (
                brand,
                brand,
                *DIRECT_EVENT_SOURCE_PROCESSORS,
                event_config.min_score_direct,
                snapshot_date,
                event_config.lookback_months,
                snapshot_date,
                event_config.max_count_direct * 3,
            ),
        )
        direct_rows = cur.fetchall()

    direct_events = [_event_row(row) for row in direct_rows]
    if (event_config.deduplication or {}).get("enabled", False):
        direct_events = _dedup_by_date(direct_events)
    direct_events = direct_events[: event_config.max_count_direct]

    brand_english = None
    keywords = brand_context.get("search_keywords") or {}
    english_values = keywords.get("약 영문명") or []
    if english_values:
        brand_english = english_values[0]
    brand_centric = []
    market_trend = []
    for event in direct_events:
        is_centric, signal = is_brand_centric(event, brand, brand_english)
        enriched = {**event, "is_brand_centric": is_centric, "matching_signal": signal}
        if is_centric:
            brand_centric.append(enriched)
        else:
            market_trend.append(enriched)

    cross = _build_event_bundle_v1(brand, db_conn, snapshot_at, event_config)["cross_match_events"]
    counts = Counter(event["tag"] for event in brand_centric + market_trend)
    return {
        "events_brand_centric": brand_centric[: event_config.brand_centric_max_count],
        "events_market_trend": market_trend[: event_config.market_trend_max_count],
        "cross_match_events": cross,
        "tag_distribution": {tag: int(counts.get(tag, 0)) for tag in TAGS},
    }


def build_event_bundle(
    brand_or_context,
    db_conn_or_snapshot,
    snapshot_or_config,
    config_or_db=None,
) -> Dict:
    if isinstance(brand_or_context, dict):
        return _build_event_bundle_v1_1(brand_or_context, db_conn_or_snapshot, snapshot_or_config, config_or_db)
    return _build_event_bundle_v1(brand_or_context, db_conn_or_snapshot, snapshot_or_config, config_or_db)
