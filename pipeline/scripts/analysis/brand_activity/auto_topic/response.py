from __future__ import annotations

from collections.abc import Iterable

from .label_rules import collapse_brand_specific_topics, single_concept_label
from .models import JsonValue, TopicDefinition


def parse_model_json(content: str) -> dict[str, JsonValue]:
    """Parse a GenOS text response into a JSON object or an invalid marker."""
    from pipeline.scripts.analysis.brand_activity.llm_topic.genos_client import parse_json_object

    return parse_json_object(content)


def normalize_axis_payload(
    payload: dict[str, JsonValue],
    *,
    scope_id: str,
    fallback_label: str,
    minimum_topics: int = 3,
    maximum_topics: int = 7,
) -> dict[str, JsonValue]:
    """Normalize a market-axis payload and quarantine missing topic arrays."""
    topics = _first_list(payload, ("topics", "topic_axis", "topic_axes", "common_topics", "topic_definitions"))
    if topics is None:
        return {"status": "quarantined_invalid_schema", "scope_id": scope_id, "reason": "missing_topics", "payload_keys": sorted(payload)}
    parsed = [_topic_item(item, index, fallback_label) for index, item in enumerate(topics, start=1) if isinstance(item, dict)]
    parsed = parsed[:maximum_topics]
    if len(parsed) < minimum_topics:
        return {
            "status": "quarantined_invalid_schema",
            "scope_id": scope_id,
            "reason": "too_few_topics",
            "topic_count": len(parsed),
            "topics": parsed,
        }
    return {
        "status": str(payload.get("status") or "ok"),
        "scope_id": str(payload.get("scope_id") or scope_id),
        "axis_version": str(payload.get("axis_version") or f"{scope_id}:draft"),
        "topics": parsed,
        "etc": payload.get("etc") if isinstance(payload.get("etc"), dict) else {"label": "기타"},
        "axis_note": str(payload.get("axis_note") or payload.get("note") or ""),
    }


def topics_from_axis(axis_payload: dict[str, JsonValue], *, fallback_label: str) -> list[TopicDefinition]:
    """Convert a normalized axis payload into TopicDefinition objects."""
    topics = axis_payload.get("topics")
    result: list[TopicDefinition] = []
    if isinstance(topics, list):
        for index, item in enumerate(topics, start=1):
            if isinstance(item, dict):
                keywords = item.get("keywords")
                result.append(
                    TopicDefinition(
                        topic_id=str(item.get("topic_id") or f"T{index}"),
                        label=str(item.get("label") or f"{fallback_label} {index}"),
                        definition=str(item.get("definition") or ""),
                        keywords=tuple(str(value) for value in keywords if isinstance(value, str)) if isinstance(keywords, list) else (),
                    )
                )
    return result or [TopicDefinition("T1", fallback_label, "fallback topic", ())]


def normalize_share_payload(
    payload: dict[str, JsonValue],
    *,
    brand: str,
    atc4: str,
    scope_id: str,
    axis_version: str,
    row_count: int,
    axis_topics: Iterable[TopicDefinition] = (),
) -> dict[str, JsonValue]:
    """Normalize a brand-share payload while preserving quarantine status."""
    shares = _first_list(payload, ("topic_shares", "topic_distribution", "topic_percentages", "shares"))
    if shares is None:
        return {
            "status": "quarantined_invalid_schema",
            "brand": brand,
            "atc4": atc4,
            "scope_id": scope_id,
            "axis_version": axis_version,
            "row_count": row_count,
            "reason": "missing_topic_shares",
            "payload_keys": sorted(payload),
        }
    label_map = axis_topic_label_map(axis_topics)
    items = [_share_item(item) for item in shares if isinstance(item, dict)]
    items, backfill_count, unmatched_labels = _backfill_share_topic_ids(items, label_map)
    positive = [item for item in items if float(item["share_pct"]) > 0.0]
    brand_specific, brand_dedup_log = _brand_specific_topics(payload)
    total = round(sum(float(item["share_pct"]) for item in [*positive, *brand_specific]), 1)
    if total > 100.0:
        scale = 100.0 / total
        positive = _scaled(positive, scale)
        brand_specific = _scaled(brand_specific, scale)
    positive, brand_specific, etc_pct = balance_share_percentages(positive, brand_specific)
    return {
        "status": str(payload.get("status") or "ok"),
        "brand": brand,
        "atc4": atc4,
        "scope_id": scope_id,
        "axis_version": str(payload.get("axis_version") or axis_version),
        "denominator": "brand_row_count_primary_topic",
        "row_count": row_count,
        "topic_shares": positive,
        "brand_specific_topics": brand_specific,
        "etc_pct": etc_pct,
        "brand_specific_dedup_count": len(brand_dedup_log),
        "brand_specific_dedup_log": brand_dedup_log,
        "topic_id_backfill_count": backfill_count,
        "unmatched_missing_topic_labels": unmatched_labels,
        "cross_insights": payload.get("cross_insights") if isinstance(payload.get("cross_insights"), dict) else {},
        "evidence_note": str(payload.get("evidence_note") or ""),
    }


def axis_topic_label_map(axis_topics: Iterable[TopicDefinition]) -> dict[str, str]:
    """Build an unambiguous normalized label-to-topic-id map for an axis."""
    label_map: dict[str, str] = {}
    ambiguous: set[str] = set()
    for topic in axis_topics:
        label_key = _normalized_topic_label(topic.label)
        if not label_key:
            continue
        existing = label_map.get(label_key)
        if existing is None:
            label_map[label_key] = topic.topic_id
        elif existing != topic.topic_id:
            ambiguous.add(label_key)
    for label_key in ambiguous:
        label_map.pop(label_key, None)
    return label_map


def balance_share_percentages(
    topic_shares: list[dict[str, JsonValue]],
    brand_specific_topics: list[dict[str, JsonValue]],
) -> tuple[list[dict[str, JsonValue]], list[dict[str, JsonValue]], float]:
    """Return shares plus 기타 with deterministic one-decimal 100 percent balance."""
    market_topics = list(topic_shares)
    brand_topics = list(brand_specific_topics)
    total = round(sum(float(item["share_pct"]) for item in [*market_topics, *brand_topics]), 1)
    if total > 100.0:
        _reduce_largest_share(market_topics, brand_topics, round(total - 100.0, 1))
        total = round(sum(float(item["share_pct"]) for item in [*market_topics, *brand_topics]), 1)
    return market_topics, brand_topics, round(max(0.0, 100.0 - total), 1)


def _topic_item(item: dict[str, JsonValue], index: int, fallback_label: str) -> dict[str, JsonValue]:
    """Normalize one untrusted topic item."""
    keywords = _first_list(item, ("keywords", "representative_keywords", "key_terms"))
    label, rewritten = single_concept_label(str(item.get("label") or item.get("topic_label") or item.get("name") or f"{fallback_label} {index}"))
    return {
        "topic_id": str(item.get("topic_id") or item.get("id") or item.get("code") or f"T{index}"),
        "label": label,
        "definition": str(item.get("definition") or item.get("description") or ""),
        "keywords": [str(keyword) for keyword in keywords if isinstance(keyword, str)] if isinstance(keywords, list) else [],
        "single_concept_rewritten": rewritten,
    }


def _share_item(item: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Normalize one untrusted share item into report fields."""
    label, rewritten = single_concept_label(str(item.get("label") or item.get("topic_label") or item.get("name") or ""))
    return {
        "topic_id": str(item.get("topic_id") or item.get("id") or ""),
        "label": label,
        "share_pct": _pct(_first_scalar(item, ("share_pct", "percentage", "pct", "share", "ratio"))),
        "row_count": int(_first_scalar(item, ("row_count", "count", "rows")) or 0),
        "single_concept_rewritten": rewritten,
    }


def _backfill_share_topic_ids(
    items: list[dict[str, JsonValue]],
    label_map: dict[str, str],
) -> tuple[list[dict[str, JsonValue]], int, list[str]]:
    """Backfill missing market-axis topic ids from exact normalized labels."""
    result: list[dict[str, JsonValue]] = []
    unmatched: list[str] = []
    backfill_count = 0
    for item in items:
        topic_id = str(item.get("topic_id") or "")
        label = str(item.get("label") or "")
        mapped_id = label_map.get(_normalized_topic_label(label), "") if not topic_id else ""
        if mapped_id:
            result.append({**item, "topic_id": mapped_id})
            backfill_count += 1
        else:
            result.append(item)
            if not topic_id and label:
                unmatched.append(label)
    return result, backfill_count, unmatched


def _reduce_largest_share(
    market_topics: list[dict[str, JsonValue]],
    brand_topics: list[dict[str, JsonValue]],
    overage: float,
) -> None:
    """Subtract small rounding overage from the largest positive share in place."""
    candidates: list[tuple[str, int, float]] = [
        ("market", index, float(item["share_pct"]))
        for index, item in enumerate(market_topics)
        if float(item["share_pct"]) > 0.0
    ]
    candidates.extend(
        ("brand", index, float(item["share_pct"]))
        for index, item in enumerate(brand_topics)
        if float(item["share_pct"]) > 0.0
    )
    if not candidates:
        return
    target_kind, target_index, target_share = max(candidates, key=lambda item: item[2])
    target_pool = market_topics if target_kind == "market" else brand_topics
    target_pool[target_index] = {**target_pool[target_index], "share_pct": round(max(0.0, target_share - overage), 1)}


def _normalized_topic_label(label: str) -> str:
    """Normalize labels for deterministic exact matching."""
    return "".join(label.split()).casefold()


def _brand_specific_topics(payload: dict[str, JsonValue]) -> tuple[list[dict[str, JsonValue]], list[dict[str, JsonValue]]]:
    """Normalize up to two brand-specific topics outside the market axis."""
    rows = _first_list(payload, ("brand_specific_topics", "brand_topics", "brand_specific_shares", "additional_brand_topics"))
    if rows is None:
        return [], []
    parsed = [_brand_topic_item(item, index) for index, item in enumerate(rows, start=1) if isinstance(item, dict)]
    return collapse_brand_specific_topics([item for item in parsed if float(item["share_pct"]) > 0.0])


def _brand_topic_item(item: dict[str, JsonValue], index: int) -> dict[str, JsonValue]:
    """Normalize one brand-specific topic share."""
    label, rewritten = single_concept_label(str(item.get("label") or item.get("topic_label") or item.get("name") or f"브랜드 특화 {index}"))
    return {
        "topic_id": str(item.get("topic_id") or item.get("id") or f"B{index}"),
        "label": label,
        "definition": str(item.get("definition") or item.get("description") or ""),
        "share_pct": _pct(_first_scalar(item, ("share_pct", "percentage", "pct", "share", "ratio"))),
        "row_count": int(_first_scalar(item, ("row_count", "count", "rows")) or 0),
        "source": "brand_specific",
        "single_concept_rewritten": rewritten,
    }


def _scaled(items: list[dict[str, JsonValue]], scale: float) -> list[dict[str, JsonValue]]:
    """Scale percentage-like share rows while preserving labels and counts."""
    return [{**item, "share_pct": round(float(item["share_pct"]) * scale, 1)} for item in items]


def _first_list(payload: dict[str, JsonValue], keys: tuple[str, ...]) -> list[JsonValue] | None:
    """Find the first list under direct keys or shallow wrappers."""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return value
    for wrapper_key in ("result", "output", "response", "data"):
        wrapped = payload.get(wrapper_key)
        if isinstance(wrapped, dict):
            nested = _first_list(wrapped, keys)
            if nested is not None:
                return nested
    return None


def _first_scalar(payload: dict[str, JsonValue], keys: tuple[str, ...]) -> JsonValue:
    """Find the first scalar value under known keys."""
    for key in keys:
        value = payload.get(key)
        if value is not None and not isinstance(value, dict | list):
            return value
    return None


def _pct(value: JsonValue) -> float:
    """Convert percentages or 0-1 ratios into percentage points."""
    if not isinstance(value, int | float):
        return 0.0
    numeric = float(value)
    if 0.0 < numeric <= 1.0:
        numeric *= 100.0
    return round(numeric, 1)
