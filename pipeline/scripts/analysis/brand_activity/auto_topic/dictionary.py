from __future__ import annotations

from .models import JsonValue, KeywordRow


def dictionary_baseline(rows: list[KeywordRow], seed_dictionary: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Measure REDESIGN dictionary hit shares for sampled rows as a QC cross-check."""
    topics = _topic_keywords(seed_dictionary)
    results: list[dict[str, JsonValue]] = []
    denominator = len(rows) or 1
    for label, keywords in topics.items():
        hits = sum(1 for row in rows if _row_has_keyword(row, keywords))
        if hits:
            results.append({"label": label, "hit_rows": hits, "share_pct": round((hits / denominator) * 100, 1)})
    return {
        "row_count": len(rows),
        "multi_label_denominator": "sampled_row_count; topic shares may exceed 100",
        "topics": sorted(results, key=lambda item: float(item["share_pct"]), reverse=True)[:8],
    }


def _topic_keywords(seed_dictionary: dict[str, JsonValue]) -> dict[str, tuple[str, ...]]:
    """Flatten seed dictionary keyword arrays by label."""
    flattened: dict[str, tuple[str, ...]] = {}
    for label, topic_value in seed_dictionary.items():
        if isinstance(topic_value, dict):
            keywords = topic_value.get("keywords")
            if isinstance(keywords, list):
                flattened[str(label)] = tuple(str(keyword) for keyword in keywords if isinstance(keyword, str))
    return flattened


def _row_has_keyword(row: KeywordRow, keywords: tuple[str, ...]) -> bool:
    """Return whether one source row contains any dictionary keyword."""
    text = row.keyword_text.lower()
    return any(keyword.lower() in text for keyword in keywords)
