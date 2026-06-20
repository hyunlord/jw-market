from __future__ import annotations

from .models import JsonValue


def stabilize_axis(previous_axis: dict[str, JsonValue] | None, new_axis: dict[str, JsonValue], *, threshold: float) -> dict[str, JsonValue]:
    """Keep the previous axis when topic similarity is high enough for continuity."""
    if not previous_axis or previous_axis.get("status") != "ok" or new_axis.get("status") != "ok":
        return {**new_axis, "stability": {"action": "initialize" if not previous_axis else "update", "similarity": None, "threshold": threshold}}
    similarity = axis_similarity(previous_axis, new_axis)
    if similarity >= threshold:
        return {
            **previous_axis,
            "stability": {
                "action": "keep",
                "similarity": similarity,
                "threshold": threshold,
                "new_axis_version": new_axis.get("axis_version"),
                "reason": "new axis matched previous axis above threshold",
            },
        }
    return {
        **new_axis,
        "axis_version": _next_axis_version(str(previous_axis.get("axis_version") or "v0")),
        "stability": {
            "action": "update",
            "similarity": similarity,
            "threshold": threshold,
            "previous_axis_version": previous_axis.get("axis_version"),
            "reason": "new axis changed below threshold",
        },
    }


def axis_similarity(left_axis: dict[str, JsonValue], right_axis: dict[str, JsonValue]) -> float:
    """Calculate average best-match token overlap between two axes."""
    left_topics = _topic_strings(left_axis)
    right_topics = _topic_strings(right_axis)
    if not left_topics or not right_topics:
        return 0.0
    scores = [_best_topic_score(topic, right_topics) for topic in left_topics]
    return round(sum(scores) / len(scores), 3)


def max_share_delta_pp(first: dict[str, JsonValue], second: dict[str, JsonValue]) -> float:
    """Return the maximum absolute share-point delta between two brand-share payloads."""
    first_map = share_map(first)
    second_map = share_map(second)
    topic_ids = set(first_map) | set(second_map)
    if not topic_ids:
        return 0.0
    return round(max(abs(first_map.get(topic_id, 0.0) - second_map.get(topic_id, 0.0)) for topic_id in topic_ids), 1)


def share_map(payload: dict[str, JsonValue]) -> dict[str, float]:
    """Extract topic share percentages including 기타 from a normalized payload."""
    result: dict[str, float] = {}
    shares = payload.get("topic_shares")
    if isinstance(shares, list):
        for item in shares:
            if isinstance(item, dict):
                result[str(item.get("topic_id") or item.get("label") or "")] = float(item.get("share_pct") or 0.0)
    brand_topics = payload.get("brand_specific_topics")
    if isinstance(brand_topics, list):
        for item in brand_topics:
            if isinstance(item, dict):
                result[str(item.get("topic_id") or item.get("label") or "")] = float(item.get("share_pct") or 0.0)
    etc = payload.get("etc_pct")
    if isinstance(etc, int | float):
        result["기타"] = float(etc)
    return result


def _topic_strings(axis: dict[str, JsonValue]) -> list[str]:
    """Flatten labels and keywords from a normalized axis for similarity scoring."""
    topics = axis.get("topics")
    values: list[str] = []
    if isinstance(topics, list):
        for item in topics:
            if isinstance(item, dict):
                keywords = item.get("keywords")
                values.append(" ".join([str(item.get("label") or ""), *(str(keyword) for keyword in keywords if isinstance(keywords, list) for keyword in keywords)]))
    return values


def _best_topic_score(topic: str, candidates: list[str]) -> float:
    """Find the highest lexical overlap for one topic string."""
    tokens = _tokens(topic)
    if not tokens:
        return 0.0
    return max((_jaccard(tokens, _tokens(candidate)) for candidate in candidates), default=0.0)


def _tokens(value: str) -> set[str]:
    """Tokenize Korean/English topic text into words and short shingles."""
    raw = [token.lower() for token in value.replace("/", " ").replace("-", " ").replace(",", " ").split()]
    tokens = {token for token in raw if token}
    for token in raw:
        if len(token) >= 3:
            tokens.update(token[index : index + 2] for index in range(len(token) - 1))
    return tokens


def _jaccard(left: set[str], right: set[str]) -> float:
    """Calculate Jaccard overlap for two token sets."""
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _next_axis_version(previous: str) -> str:
    """Increment the final integer in an axis version or append v2."""
    digits = ""
    for char in reversed(previous):
        if char.isdigit():
            digits = char + digits
        else:
            break
    if not digits:
        return f"{previous}:v2"
    return f"{previous[: -len(digits)]}{int(digits) + 1}"
