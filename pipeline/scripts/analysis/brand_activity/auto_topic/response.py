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


def brand_specific_topics_from_payload(payload: dict[str, JsonValue], *, fallback_label: str) -> list[TopicDefinition]:
    """Convert a definition-only brand-specific payload into up to two topics."""
    rows = _first_list(payload, ("brand_specific_topics", "brand_topics", "topic_definitions", "topics"))
    if rows is None:
        return []
    topics: list[TopicDefinition] = []
    seen_labels: set[str] = set()
    for index, item in enumerate(rows, start=1):
        if not isinstance(item, dict):
            continue
        label, _rewritten = single_concept_label(str(item.get("label") or item.get("topic_label") or item.get("name") or f"{fallback_label} {index}"))
        label_key = _normalized_topic_label(label)
        if not label_key or label_key in seen_labels:
            continue
        seen_labels.add(label_key)
        keywords = _first_list(item, ("keywords", "representative_keywords", "key_terms"))
        topics.append(
            TopicDefinition(
                topic_id=str(item.get("topic_id") or item.get("id") or f"B{len(topics) + 1}"),
                label=label,
                definition=str(item.get("definition") or item.get("description") or ""),
                keywords=tuple(str(value) for value in keywords if isinstance(value, str)) if isinstance(keywords, list) else (),
            )
        )
        if len(topics) >= 2:
            break
    return topics


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
    positive = [item for item in items if int(item["affected_row_count"]) > 0]
    brand_specific, brand_dedup_log = _brand_specific_topics(payload)
    total_rows = max(0, int(row_count))
    positive = [_with_share_pct(item, total_rows) for item in positive]
    brand_specific = [_with_share_pct(item, total_rows) for item in brand_specific]
    return {
        "status": str(payload.get("status") or "ok"),
        "brand": brand,
        "atc4": atc4,
        "scope_id": scope_id,
        "axis_version": str(payload.get("axis_version") or axis_version),
        "denominator": "brand_total_row_count",
        "row_count": row_count,
        "topic_shares": positive,
        "brand_specific_topics": brand_specific,
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
    affected = int(_first_scalar(item, ("affected_row_count", "row_count", "count", "rows")) or 0)
    return {
        "topic_id": str(item.get("topic_id") or item.get("id") or ""),
        "label": label,
        "affected_row_count": max(0, affected),
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


def _normalized_topic_label(label: str) -> str:
    """Normalize labels for deterministic exact matching."""
    return "".join(label.split()).casefold()


def _brand_specific_topics(payload: dict[str, JsonValue]) -> tuple[list[dict[str, JsonValue]], list[dict[str, JsonValue]]]:
    """Normalize up to two brand-specific topics outside the market axis."""
    rows = _first_list(payload, ("brand_specific_topics", "brand_topics", "brand_specific_shares", "additional_brand_topics"))
    if rows is None:
        return [], []
    parsed = [_brand_topic_item(item, index) for index, item in enumerate(rows, start=1) if isinstance(item, dict)]
    return collapse_brand_specific_topics([item for item in parsed if int(item["affected_row_count"]) > 0])


def _brand_topic_item(item: dict[str, JsonValue], index: int) -> dict[str, JsonValue]:
    """Normalize one brand-specific topic share."""
    label, rewritten = single_concept_label(str(item.get("label") or item.get("topic_label") or item.get("name") or f"브랜드 특화 {index}"))
    affected = int(_first_scalar(item, ("affected_row_count", "row_count", "count", "rows")) or 0)
    return {
        "topic_id": str(item.get("topic_id") or item.get("id") or f"B{index}"),
        "label": label,
        "definition": str(item.get("definition") or item.get("description") or ""),
        "affected_row_count": max(0, affected),
        "source": "brand_specific",
        "single_concept_rewritten": rewritten,
    }


def _with_share_pct(item: dict[str, JsonValue], total_rows: int) -> dict[str, JsonValue]:
    """Derive independent influence percentage from affected rows."""
    affected = min(max(0, int(item.get("affected_row_count") or 0)), total_rows) if total_rows > 0 else 0
    share_pct = round(affected * 100.0 / total_rows, 1) if total_rows > 0 else 0.0
    return {**item, "affected_row_count": affected, "share_pct": share_pct}


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
