from __future__ import annotations

from .models import JsonValue, TopicDefinition


def parse_model_json(content: str) -> dict[str, JsonValue]:
    """Parse GenOS content into a JSON object or an invalid-marker object."""
    from pipeline.scripts.analysis.brand_activity.llm_topic.genos_client import parse_json_object

    return parse_json_object(content)


def topics_from_payload(payload: dict[str, JsonValue], fallback_label: str) -> list[TopicDefinition]:
    """Extract topic definitions from a model axis payload."""
    topics = _first_list(payload, ("topics", "topic_axis", "topic_axes", "common_topics", "topic_definitions"))
    if topics is None:
        return [TopicDefinition("T1", fallback_label, "fallback axis", ())]
    parsed: list[TopicDefinition] = []
    for index, item in enumerate(topics, start=1):
        if isinstance(item, dict):
            keywords = _first_list(item, ("keywords", "representative_keywords", "key_terms"))
            parsed.append(
                TopicDefinition(
                    topic_id=str(item.get("topic_id") or item.get("id") or item.get("code") or f"T{index}"),
                    label=str(item.get("label") or item.get("topic_label") or item.get("name") or f"Topic {index}"),
                    definition=str(item.get("definition") or item.get("description") or ""),
                    keywords=tuple(str(value) for value in keywords if isinstance(value, str)) if isinstance(keywords, list) else (),
                )
            )
    return parsed or [TopicDefinition("T1", fallback_label, "fallback axis", ())]


def normalize_share_payload(payload: dict[str, JsonValue], *, brand: str, scope_id: str, row_count: int) -> dict[str, JsonValue]:
    """Normalize brand-share payloads so percentages sum to 100 with 기타."""
    shares = _first_list(payload, ("topic_shares", "topic_distribution", "topic_percentages", "shares"))
    if shares is None:
        return {
            "status": "quarantined_invalid_schema",
            "brand": brand,
            "scope_id": scope_id,
            "row_count": row_count,
            "payload_keys": sorted(payload.keys()),
        }
    items = [_share_item(item) for item in shares if isinstance(item, dict)]
    positive = [item for item in items if item["share_pct"] > 0]
    total = round(sum(item["share_pct"] for item in positive), 1)
    if total > 100.0:
        scale = 100.0 / total
        positive = [{**item, "share_pct": round(item["share_pct"] * scale, 1)} for item in positive]
    etc_pct = _first_scalar(payload, ("etc_pct", "other_pct", "etc_share", "other_share"))
    if isinstance(etc_pct, int | float):
        etc = _pct(etc_pct)
    else:
        etc = round(max(0.0, 100.0 - sum(item["share_pct"] for item in positive)), 1)
    return {
        "status": str(payload.get("status") or "ok"),
        "brand": brand,
        "scope_id": scope_id,
        "axis_version": str(payload.get("axis_version") or scope_id),
        "denominator": "brand_row_count_primary_topic",
        "row_count": row_count,
        "topic_shares": positive,
        "etc_pct": etc,
        "cross_insights": payload.get("cross_insights") if isinstance(payload.get("cross_insights"), dict) else {},
        "evidence_note": str(payload.get("evidence_note") or ""),
    }


def _share_item(item: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Convert one untrusted share item into the report shape."""
    return {
        "topic_id": str(item.get("topic_id") or item.get("id") or item.get("label") or ""),
        "label": str(item.get("label") or item.get("topic_label") or item.get("name") or ""),
        "share_pct": _pct(_first_scalar(item, ("share_pct", "percentage", "pct", "share", "ratio"))),
        "row_count": int(_first_scalar(item, ("row_count", "count", "rows")) or 0),
    }


def _first_list(payload: dict[str, JsonValue], keys: tuple[str, ...]) -> list[JsonValue] | None:
    """Find the first list under known keys, including shallow result wrappers."""
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
    """Find the first non-container scalar under known keys."""
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        if not isinstance(value, dict | list):
            return value
    return None


def _pct(value: JsonValue) -> float:
    """Convert model percentages or 0-1 ratios into rounded percentage points."""
    if not isinstance(value, int | float):
        return 0.0
    numeric = float(value)
    if 0.0 < numeric <= 1.0:
        numeric *= 100.0
    return round(numeric, 1)
