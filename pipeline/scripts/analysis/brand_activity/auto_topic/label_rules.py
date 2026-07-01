from __future__ import annotations

import re
from typing import Final

from .models import JsonValue


FORBIDDEN_LABEL_CONNECTORS: Final = ("및", "/", ",")
_CONNECTOR_RE: Final = re.compile(r"\s*(?:및|/|,)\s*")
_TOKEN_SPLIT_RE: Final = re.compile(r"[\s/,\-·]+")


def single_concept_label(label: str) -> tuple[str, bool]:
    """Return a one-concept label by keeping the first connector-delimited concept."""
    stripped = label.strip()
    if not has_complex_connector(stripped):
        return stripped, False
    parts = [part.strip() for part in _CONNECTOR_RE.split(stripped) if part.strip()]
    return (parts[0] if parts else stripped, True)


def has_complex_connector(label: str) -> bool:
    """Return true when a topic label joins concepts with banned connectors."""
    return any(connector in label for connector in FORBIDDEN_LABEL_CONNECTORS)


def collapse_brand_specific_topics(rows: list[dict[str, JsonValue]]) -> tuple[list[dict[str, JsonValue]], list[dict[str, JsonValue]]]:
    """Merge near-duplicate brand-specific topics and keep at most two concepts."""
    kept: list[dict[str, JsonValue]] = []
    merge_log: list[dict[str, JsonValue]] = []
    for row in rows:
        clean_label, rewritten = single_concept_label(str(row.get("label") or ""))
        candidate = {**row, "label": clean_label}
        if rewritten:
            candidate["single_concept_rewritten"] = True
        match_index = _matching_topic_index(kept, clean_label)
        if match_index is None:
            if len(kept) < 2:
                kept.append(candidate)
            continue
        kept[match_index] = _merge_topic(kept[match_index], candidate)
        merge_log.append(
            {
                "kept_label": str(kept[match_index].get("label") or ""),
                "dropped_label": clean_label,
                "reason": "near_duplicate_brand_specific_label",
            }
        )
    return _renumber_brand_topics(kept), merge_log


def label_quality_summary(axis_results: dict[str, JsonValue], brand_results: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Summarize complex-label and brand-specific duplicate violations."""
    complex_rows = _complex_label_rows(axis_results, brand_results)
    duplicate_rows = _brand_specific_duplicate_rows(brand_results)
    return {
        "complex_label_count": len(complex_rows),
        "complex_labels": complex_rows,
        "brand_specific_duplicate_pair_count": len(duplicate_rows),
        "brand_specific_duplicate_pairs": duplicate_rows,
    }


def are_near_duplicate_labels(left: str, right: str) -> bool:
    """Return true when two labels represent the same brand-specific concept."""
    left_key = _concept_key(left)
    right_key = _concept_key(right)
    if not left_key or not right_key:
        return False
    if left_key == right_key:
        return True
    if min(len(left_key), len(right_key)) >= 4 and (left_key in right_key or right_key in left_key):
        return True
    left_tokens = _semantic_tokens(left)
    right_tokens = _semantic_tokens(right)
    if not left_tokens or not right_tokens:
        return False
    overlap = len(left_tokens & right_tokens) / min(len(left_tokens), len(right_tokens))
    return overlap >= 0.75


def _matching_topic_index(rows: list[dict[str, JsonValue]], label: str) -> int | None:
    """Find the kept brand-specific topic that matches a new label."""
    for index, row in enumerate(rows):
        if are_near_duplicate_labels(str(row.get("label") or ""), label):
            return index
    return None


def _merge_topic(left: dict[str, JsonValue], right: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Merge duplicate brand-specific topics by affected row counts."""
    merged = int(left.get("affected_row_count") or left.get("row_count") or 0) + int(right.get("affected_row_count") or right.get("row_count") or 0)
    return {**left, "affected_row_count": merged}


def _renumber_brand_topics(rows: list[dict[str, JsonValue]]) -> list[dict[str, JsonValue]]:
    """Assign stable B1/B2 ids after brand-specific deduplication."""
    return [{**row, "topic_id": f"B{index}"} for index, row in enumerate(rows, start=1)]


def _complex_label_rows(axis_results: dict[str, JsonValue], brand_results: dict[str, JsonValue]) -> list[dict[str, JsonValue]]:
    """Collect labels that still contain banned connectors."""
    rows: list[dict[str, JsonValue]] = []
    for scope_key, axis in axis_results.items():
        for topic in _list(_dict(axis).get("topics")):
            _append_complex_row(rows, scope_key, "axis", _dict(topic))
    for sample_key, brand in brand_results.items():
        for topic in _list(_dict(brand).get("topic_shares")):
            _append_complex_row(rows, sample_key, "topic_share", _dict(topic))
        for topic in _list(_dict(brand).get("brand_specific_topics")):
            _append_complex_row(rows, sample_key, "brand_specific", _dict(topic))
    return rows


def _brand_specific_duplicate_rows(brand_results: dict[str, JsonValue]) -> list[dict[str, JsonValue]]:
    """Collect near-duplicate brand-specific topic pairs in measured payloads."""
    rows: list[dict[str, JsonValue]] = []
    for sample_key, brand in brand_results.items():
        topics = [_dict(topic) for topic in _list(_dict(brand).get("brand_specific_topics"))]
        if len(topics) < 2:
            continue
        left = str(topics[0].get("label") or "")
        right = str(topics[1].get("label") or "")
        if are_near_duplicate_labels(left, right):
            rows.append({"sample_key": sample_key, "left_label": left, "right_label": right})
    return rows


def _append_complex_row(rows: list[dict[str, JsonValue]], owner_key: str, label_kind: str, topic: dict[str, JsonValue]) -> None:
    """Append one complex-label row when a topic label violates connector policy."""
    label = str(topic.get("label") or "")
    if has_complex_connector(label):
        rows.append({"owner_key": owner_key, "label_kind": label_kind, "label": label})


def _concept_key(label: str) -> str:
    """Normalize a label for duplicate comparison."""
    clean, _rewritten = single_concept_label(label)
    return "".join(_semantic_tokens(clean)).casefold()


def _semantic_tokens(label: str) -> set[str]:
    """Tokenize a Korean topic label by visible separators."""
    clean, _rewritten = single_concept_label(label)
    return {token.casefold() for token in _TOKEN_SPLIT_RE.split(clean) if token}


def _dict(value: JsonValue) -> dict[str, JsonValue]:
    """Return a JSON object or an empty object."""
    return value if isinstance(value, dict) else {}


def _list(value: JsonValue) -> list[JsonValue]:
    """Return a JSON array or an empty array."""
    return value if isinstance(value, list) else []
