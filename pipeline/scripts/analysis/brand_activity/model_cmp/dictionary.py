from __future__ import annotations

import json
from pathlib import Path

from .models import JsonValue, KeywordRow


def load_redesign_dictionary(path: Path) -> dict[str, JsonValue]:
    """Load the REDESIGN seed dictionary used only as a comparison baseline."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def seed_for_atc4_values(dictionary: dict[str, JsonValue], atc4_values: tuple[str, ...]) -> dict[str, JsonValue]:
    """Select dictionary seeds for one ATC4 or multi-ATC4 group scope."""
    return {atc4: dictionary.get(atc4, {}) for atc4 in atc4_values}


def dictionary_baseline(rows: list[KeywordRow], seed_dictionary: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Calculate lightweight dictionary hit shares for sampled rows."""
    topics = _topic_keywords(seed_dictionary)
    results: list[dict[str, JsonValue]] = []
    for label, keywords in topics.items():
        hits = sum(1 for row in rows if _row_has_keyword(row, keywords))
        if hits:
            results.append({"label": label, "hit_rows": hits, "share_pct": round((hits / len(rows)) * 100, 1)})
    return {
        "row_count": len(rows),
        "multi_label_denominator": "sampled_row_count; topic shares may exceed 100%",
        "topics": sorted(results, key=lambda item: float(item["share_pct"]), reverse=True)[:8],
    }


def _topic_keywords(seed_dictionary: dict[str, JsonValue]) -> dict[str, tuple[str, ...]]:
    """Flatten REDESIGN topic keyword lists across one or more ATC4 dictionaries."""
    flattened: dict[str, tuple[str, ...]] = {}
    for atc4_value in seed_dictionary.values():
        if isinstance(atc4_value, dict):
            for label, topic_value in atc4_value.items():
                if isinstance(topic_value, dict):
                    keywords = topic_value.get("keywords")
                    if isinstance(keywords, list):
                        flattened[str(label)] = tuple(str(keyword) for keyword in keywords if isinstance(keyword, str))
    return flattened


def _row_has_keyword(row: KeywordRow, keywords: tuple[str, ...]) -> bool:
    """Return whether a sampled source row contains any dictionary keyword."""
    text = row.keyword_text.lower()
    return any(keyword.lower() in text for keyword in keywords)
