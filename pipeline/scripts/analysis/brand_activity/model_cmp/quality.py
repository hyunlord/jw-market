from __future__ import annotations

from .models import JsonValue


def topic_overlap_score(left_labels: list[str], right_labels: list[str]) -> float:
    """Score best-token overlap between two model topic-axis label lists."""
    if not left_labels or not right_labels:
        return 0.0
    scores = [_best_label_score(label, right_labels) for label in left_labels]
    return round(sum(scores) / len(scores), 3)


def max_share_delta_pp(first: dict[str, float], second: dict[str, float]) -> float:
    """Return the maximum absolute share-point drift between repeat outputs."""
    topic_ids = set(first) | set(second)
    if not topic_ids:
        return 0.0
    return round(max(abs(first.get(topic_id, 0.0) - second.get(topic_id, 0.0)) for topic_id in topic_ids), 1)


def share_map(payload: dict[str, JsonValue]) -> dict[str, float]:
    """Extract topic share percentages from a parsed brand-share payload."""
    shares = payload.get("topic_shares")
    if not isinstance(shares, list):
        return {}
    result: dict[str, float] = {}
    for item in shares:
        if isinstance(item, dict):
            topic_id = str(item.get("topic_id") or item.get("label") or "")
            result[topic_id] = float(item.get("share_pct") or 0.0)
    etc = payload.get("etc_pct")
    if isinstance(etc, int | float):
        result["기타"] = float(etc)
    return result


def _best_label_score(label: str, candidates: list[str]) -> float:
    """Find the best overlap score for one label against candidate labels."""
    tokens = _label_tokens(label)
    if not tokens:
        return 0.0
    return max((_jaccard(tokens, _label_tokens(candidate)) for candidate in candidates), default=0.0)


def _label_tokens(label: str) -> set[str]:
    """Tokenize Korean/English topic labels into comparable lexical units."""
    raw_tokens = [token.lower() for token in label.replace("/", " ").replace("-", " ").split()]
    tokens = {token for token in raw_tokens if token}
    for token in raw_tokens:
        if len(token) >= 3:
            tokens.update(token[index : index + 2] for index in range(len(token) - 1))
    return tokens


def _jaccard(left_tokens: set[str], right_tokens: set[str]) -> float:
    """Calculate Jaccard overlap for label-token sets."""
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
