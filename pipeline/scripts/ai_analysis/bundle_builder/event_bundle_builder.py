from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, Iterable, Optional, Tuple

from pipeline.etl.io.mart.agent2_eligibility import (
    AGENT2_CROSS_PROCESSORS,
    AGENT2_DIRECT_PROCESSORS,
    Agent2ScoreRow,
    eligible_agent2_events,
    is_agent2_eligible,
)
from pipeline.scripts.ai_analysis.agent2_brand_registry import (
    Agent2BrandRegistry,
    event_brand_match_sql,
)

from .agent2_effective_selector import (
    EffectiveSelectorConfig,
    select_effective_agent2_events,
)
from .config import EventConfig

TAGS = ["신약/R&D", "정책/규제", "공급/생산", "자본/경영", "외부/트렌드", "기타"]
DIRECT_EVENT_SOURCE_PROCESSORS = AGENT2_DIRECT_PROCESSORS
CROSS_MATCH_SOURCE_PROCESSORS = AGENT2_CROSS_PROCESSORS
# Tier2 processor policy:
# - tier2_llm_v1 is LLM-confirmed brand/article evidence and is visible to Agent2.
# - tier2_exact_rule_v1 is search/exact-rule provenance only and intentionally
#   stays outside Agent2 narrative evidence.
# - tier2_llm_v2_rev5671 is visible after its calibrated category mapping was
#   activated atomically with serving policy.


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


@dataclass(frozen=True, slots=True)
class CentralBundleRows:
    direct_rows: tuple[dict[str, Any], ...]
    cross_rows: tuple[dict[str, Any], ...]
    selector_revision: str


def _score_row(row: dict[str, Any]) -> Agent2ScoreRow:
    return Agent2ScoreRow(
        news_id=str(row.get("news_id") or "").strip(),
        source_processor=str(row.get("source_processor") or "").strip() or None,
        derivation=str(row.get("derivation") or "").strip() or None,
        tag=str(row.get("tag") or "").strip() or None,
        score=row.get("score") if row.get("score") is not None else 0,
        published_date=row.get("published_date"),
        news_exists=row.get("joined_news_id") is not None,
    )


def _best_rows_by_news_id(
    rows: Iterable[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    def row_order(row: dict[str, Any]) -> tuple[float, int, str, int, str, str]:
        published = row.get("published_date")
        if isinstance(published, datetime):
            published = published.date()
        published_ordinal = published.toordinal() if isinstance(published, date) else 0
        derivation = str(row.get("derivation") or "")
        return (
            -float(row.get("score") or 0),
            -published_ordinal,
            str(row.get("news_id") or ""),
            0 if derivation == "llm_direct" else 1,
            str(row.get("source_processor") or ""),
            str(row.get("tag") or ""),
        )

    ordered = sorted(
        rows,
        key=row_order,
    )
    result: dict[str, dict[str, Any]] = {}
    for row in ordered:
        result.setdefault(str(row["news_id"]), row)
    return result


def select_central_bundle_rows(
    rows: Iterable[dict[str, Any]],
    *,
    snapshot_date: date,
    lookback_months: int,
    direct_cap: int,
    cross_cap: int,
    deduplicate_direct_by_date: bool,
) -> CentralBundleRows:
    """Apply the central eligibility and effective-selection contracts."""

    scored_rows = tuple((row, _score_row(row)) for row in rows)
    eligible_rows = tuple(
        (row, score_row)
        for row, score_row in scored_rows
        if is_agent2_eligible(score_row)
    )
    eligible = eligible_agent2_events(
        score_row for _, score_row in eligible_rows
    )
    config = EffectiveSelectorConfig(
        lookback_months=lookback_months,
        direct_prefetch=max(direct_cap * 3, direct_cap),
        direct_cap=direct_cap,
        cross_cap=cross_cap,
        deduplicate_direct_by_date=deduplicate_direct_by_date,
    )
    selection = select_effective_agent2_events(
        eligible,
        snapshot_date=snapshot_date,
        config=config,
    )
    rows_by_id = _best_rows_by_news_id(row for row, _ in eligible_rows)
    return CentralBundleRows(
        direct_rows=tuple(
            rows_by_id[news_id] for news_id in selection.selected_direct_news_ids
        ),
        cross_rows=tuple(
            rows_by_id[news_id] for news_id in selection.selected_cross_news_ids
        ),
        selector_revision=selection.selector_revision,
    )


def _fetch_central_bundle_rows(
    brand: str,
    db_conn: Any,
    snapshot_date: date,
    config: EventConfig,
) -> CentralBundleRows:
    registry = Agent2BrandRegistry.for_canonical_names(
        {brand},
    )
    names = registry.source_names_for(brand)
    brand_predicate, brand_params = event_brand_match_sql(names)
    with db_conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT s.news_id, n.news_id AS joined_news_id, n.published_date,
                   s.score, s.tag, n.title, s.summary, s.reason,
                   n.source_name, s.source_processor, s.derivation,
                   s.mirrored_from_jw_brands
            FROM event_brand_scores s
            LEFT JOIN news_raw n ON s.news_id = n.news_id
            WHERE ({brand_predicate})
            """,
            brand_params,
        )
        rows = cur.fetchall()
    return select_central_bundle_rows(
        rows,
        snapshot_date=snapshot_date,
        lookback_months=config.lookback_months,
        direct_cap=config.max_count_direct,
        cross_cap=config.max_count_cross,
        deduplicate_direct_by_date=bool(
            (config.deduplication or {}).get("enabled", False)
        ),
    )


def _build_event_bundle_v1(
    brand: str,
    db_conn,
    snapshot_at,
    config: EventConfig,
) -> Dict:
    selected = _fetch_central_bundle_rows(
        brand,
        db_conn,
        snapshot_at.date(),
        config,
    )
    direct_events = [_event_row(row) for row in selected.direct_rows]
    cross_events = [
        _event_row(row, include_reason=False, include_mirror=True)
        for row in selected.cross_rows
    ]
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


def _build_event_bundle_v1_1(
    brand_context: dict,
    snapshot_at,
    config,
    db_conn,
) -> Dict:
    event_config = config.event
    brand = brand_context["name"]
    selected = _fetch_central_bundle_rows(
        brand,
        db_conn,
        snapshot_at.date(),
        event_config,
    )
    direct_events = [_event_row(row) for row in selected.direct_rows]

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

    cross = [
        _event_row(row, include_reason=False, include_mirror=True)
        for row in selected.cross_rows
    ]
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
