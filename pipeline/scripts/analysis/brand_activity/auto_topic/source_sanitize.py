from __future__ import annotations

from collections.abc import Sequence

from .models import JsonValue, KeywordRow


MIN_SOURCE_TEXT_CHARS = 24
NEUTRAL_DEFINITION_SUFFIX = "관련 브랜드 고유 메시지(원문 인용 제거)"


def sanitize_source_text_carryover(payload: dict[str, JsonValue], rows: Sequence[KeywordRow]) -> dict[str, JsonValue]:
    """Remove copied source text from brand-specific label/definition fields in-place."""
    needles = _source_needles(rows)
    if not needles:
        return {"sanitized_topic_count": 0, "sanitized_fields": []}
    sanitized_count = 0
    sanitized_fields: set[str] = set()
    for brand_payload in _dict(payload.get("brand_results")).values():
        for topic in _list(_dict(brand_payload).get("brand_specific_topics")):
            topic_dict = _dict(topic)
            fields = _sanitize_topic_fields(topic_dict, needles)
            if fields:
                sanitized_count += 1
                sanitized_fields.update(fields)
    return {"sanitized_topic_count": sanitized_count, "sanitized_fields": sorted(sanitized_fields)}


def _sanitize_topic_fields(topic: dict[str, JsonValue], needles: Sequence[str]) -> list[str]:
    fields: list[str] = []
    label = str(topic.get("label") or "")
    if _contains_source_text(label, needles):
        topic["label"] = "브랜드 특화 메시지"
        fields.append("label")
        label = "브랜드 특화 메시지"
    definition = str(topic.get("definition") or "")
    if _contains_source_text(definition, needles):
        topic["definition"] = f"'{label}' {NEUTRAL_DEFINITION_SUFFIX}"
        fields.append("definition")
    if fields:
        topic["sanitized"] = True
        topic["sanitized_fields"] = fields
    return fields


def _source_needles(rows: Sequence[KeywordRow]) -> list[str]:
    needles: set[str] = set()
    for row in rows:
        text = _normalize(row.keyword_text)
        if len(text) >= MIN_SOURCE_TEXT_CHARS:
            needles.add(text)
    return sorted(needles)


def _contains_source_text(value: str, needles: Sequence[str]) -> bool:
    normalized = _normalize(value)
    return any(needle in normalized for needle in needles)


def _normalize(value: str) -> str:
    return " ".join(value.split())


def _dict(value: JsonValue) -> dict[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def _list(value: JsonValue) -> list[JsonValue]:
    return value if isinstance(value, list) else []
