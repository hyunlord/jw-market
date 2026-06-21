"""Data profiling primitives for topic extraction inputs."""

from __future__ import annotations

from collections import Counter, defaultdict
from statistics import median

from .models import JsonValue, MessageRecord
from .text_tokens import language_bucket, ngram_counts, token_counts


def deduplicate_messages(rows: list[MessageRecord]) -> list[MessageRecord]:
    """Collapse duplicate market/message text pairs while preserving frequency."""
    buckets: dict[tuple[str, str], MessageRecord] = {}
    weights: defaultdict[tuple[str, str], int] = defaultdict(int)
    periods: defaultdict[tuple[str, str], list[str]] = defaultdict(list)
    for row in rows:
        key = (row.market, row.message_text)
        weights[key] += row.frequency
        periods[key].append(row.period_ym)
        buckets.setdefault(key, row)
    deduped = []
    for key, base in buckets.items():
        deduped.append(
            MessageRecord(
                base.source,
                base.market,
                base.message_hash,
                min(periods[key]),
                base.product_name,
                base.message_text,
                weights[key],
            )
        )
    return sorted(deduped, key=lambda item: (-item.frequency, item.market, item.message_hash))


def percentile(values: list[int], q: float) -> int:
    """Return an integer nearest-rank percentile for compact report tables."""
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * q)))
    return ordered[index]


def profile_market(rows: list[MessageRecord]) -> dict[str, JsonValue]:
    """Profile one market's message volume, language, length, tokens, and n-grams."""
    lengths = [len(row.message_text) for row in rows]
    unique_texts = {row.message_text for row in rows}
    languages = Counter(language_bucket(row.message_text) for row in rows)
    messages = [row.message_text for row in rows]
    tokens = token_counts(messages).most_common(30)
    bigrams = ngram_counts(messages, 2).most_common(20)
    trigrams = ngram_counts(messages, 3).most_common(15)
    total = len(rows)
    return {
        "rows": total,
        "unique_messages": len(unique_texts),
        "duplicate_rate": round(1 - (len(unique_texts) / total), 4) if total else 0.0,
        "language_counts": dict(languages),
        "length_min": min(lengths) if lengths else 0,
        "length_median": int(median(lengths)) if lengths else 0,
        "length_p90": percentile(lengths, 0.9),
        "length_max": max(lengths) if lengths else 0,
        "top_tokens": tokens,
        "top_bigrams": bigrams,
        "top_trigrams": trigrams,
    }


def profile_by_market(rows: list[MessageRecord]) -> dict[str, dict[str, JsonValue]]:
    """Profile every market independently."""
    by_market: defaultdict[str, list[MessageRecord]] = defaultdict(list)
    for row in rows:
        by_market[row.market].append(row)
    return {market: profile_market(items) for market, items in sorted(by_market.items())}
