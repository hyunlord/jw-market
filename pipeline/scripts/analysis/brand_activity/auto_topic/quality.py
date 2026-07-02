from __future__ import annotations

from itertools import combinations

from .models import JsonValue
from .stability import share_map


def mechanical_guard(share_payload: dict[str, JsonValue], *, valid_topic_ids: set[str], brand_total_rows: int | None = None) -> dict[str, JsonValue]:
    """Validate schema, topic ids, and independent influence bounds."""
    reasons: list[str] = []
    if str(share_payload.get("status") or "") != "ok":
        reasons.append("schema_quarantine")
    shares = share_payload.get("topic_shares")
    if not isinstance(shares, list):
        reasons.append("missing_topic_shares")
        shares = []
    total_rows = brand_total_rows if isinstance(brand_total_rows, int) else int(share_payload.get("row_count") or 0)
    for item in shares:
        if not isinstance(item, dict):
            reasons.append("invalid_share_item")
            continue
        topic_id = str(item.get("topic_id") or "")
        pct = float(item.get("share_pct") or 0.0)
        if topic_id not in valid_topic_ids:
            reasons.append("unknown_topic_id")
        if pct < 0 or pct > 100:
            reasons.append("share_pct_out_of_bounds")
        affected = int(item.get("affected_row_count") or 0)
        if affected < 0 or (total_rows > 0 and affected > total_rows):
            reasons.append("affected_row_count_out_of_bounds")
    brand_topics = share_payload.get("brand_specific_topics")
    if isinstance(brand_topics, list):
        if len(brand_topics) > 2:
            reasons.append("too_many_brand_specific_topics")
        for item in brand_topics:
            if not isinstance(item, dict):
                reasons.append("invalid_brand_specific_item")
                continue
            pct = float(item.get("share_pct") or 0.0)
            if pct < 0 or pct > 100:
                reasons.append("brand_specific_pct_out_of_bounds")
            affected = int(item.get("affected_row_count") or 0)
            if affected < 0 or (total_rows > 0 and affected > total_rows):
                reasons.append("affected_row_count_out_of_bounds")
    return {"layer": "mechanical_guard", "status": "fail" if reasons else "pass", "reasons": sorted(set(reasons))}


def drift_check(current_payload: dict[str, JsonValue], previous_payload: dict[str, JsonValue] | None, *, threshold_pp: float = 20.0) -> dict[str, JsonValue]:
    """Flag large share movement versus a previous batch payload without blocking automation."""
    if previous_payload is None:
        return {"layer": "drift", "status": "skip_new_brand", "max_delta_pp": None, "threshold_pp": threshold_pp}
    current = share_map(current_payload)
    previous = share_map(previous_payload)
    topic_ids = set(current) | set(previous)
    max_delta = round(max((abs(current.get(topic_id, 0.0) - previous.get(topic_id, 0.0)) for topic_id in topic_ids), default=0.0), 1)
    return {"layer": "drift", "status": "flag" if max_delta > threshold_pp else "pass", "max_delta_pp": max_delta, "threshold_pp": threshold_pp}


def dictionary_cross_check(share_payload: dict[str, JsonValue], dictionary_payload: dict[str, JsonValue], *, min_overlap: float = 0.25) -> dict[str, JsonValue]:
    """Compare LLM top labels with REDESIGN dictionary top labels as a non-blocking QC flag."""
    llm_labels = _top_labels(share_payload.get("topic_shares"))
    dictionary_labels = _top_labels(dictionary_payload.get("topics"))
    if not llm_labels or not dictionary_labels:
        return {"layer": "dict_xcheck", "status": "skip_sparse", "overlap": None}
    overlap = _label_overlap(llm_labels, dictionary_labels)
    return {"layer": "dict_xcheck", "status": "flag" if overlap < min_overlap else "pass", "overlap": overlap, "llm_top": llm_labels, "dict_top": dictionary_labels}


def competitor_separation(brand_payloads: list[dict[str, JsonValue]]) -> dict[str, JsonValue]:
    """Measure whether sampled competitors in a large market have distinguishable topic vectors."""
    if len(brand_payloads) < 2:
        return {"status": "skip_single_brand", "min_distance": None}
    distances = [_vector_distance(left, right) for left, right in combinations(brand_payloads, 2)]
    min_distance = round(min(distances), 1) if distances else 0.0
    return {"status": "pass" if min_distance >= 10.0 else "weak", "min_distance": min_distance, "pair_count": len(distances)}


def quality_summary(axis_results: dict[str, JsonValue], brand_results: dict[str, JsonValue], *, large_markets: tuple[str, ...], scope_metadata: dict[str, JsonValue] | None = None) -> dict[str, JsonValue]:
    """Grade market quality from measured axes, sampled shares, and QC evidence."""
    by_market: dict[str, list[dict[str, JsonValue]]] = {}
    for payload in brand_results.values():
        if isinstance(payload, dict):
            by_market.setdefault(str(payload.get("scope_key") or payload.get("atc4") or ""), []).append(payload)
    metadata = _scope_metadata(scope_metadata)
    markets = sorted({*axis_results.keys(), *by_market.keys()})
    market_rows: list[dict[str, JsonValue]] = []
    for market in markets:
        axis = axis_results.get(market)
        brand_payloads = by_market.get(market, [])
        grade, reasons = _grade_market(axis if isinstance(axis, dict) else {}, brand_payloads, is_large=market in large_markets)
        meta = metadata.get(market, {})
        market_rows.append(
            {
                "atc4": market,
                "scope_key": market,
                "scope_id": meta.get("scope_id") or _axis_value(axis, "scope_id") or market,
                "scope_type": meta.get("scope_type") or "atc4",
                "display_name": meta.get("display_name") or _axis_value(axis, "display_name") or market,
                "atc4_values": meta.get("atc4_values") if isinstance(meta.get("atc4_values"), list) else _axis_value(axis, "atc4_values") or [market],
                "axis_row_count": _axis_value(axis, "source_row_count"),
                "quality_grade": grade,
                "reasons": reasons,
                "sampled_brand_count": len(brand_payloads),
            }
        )
    distribution: dict[str, int] = {}
    for row in market_rows:
        grade = str(row["quality_grade"])
        distribution[grade] = distribution.get(grade, 0) + 1
    return {
        "markets": market_rows,
        "grade_distribution": {grade: distribution.get(grade, 0) for grade in ("A", "B", "C", "D")},
        "large_market_competitor_separation": {
            market: competitor_separation(by_market.get(market, []))
            for market in large_markets
            if market in by_market
        },
    }


def _scope_metadata(value: dict[str, JsonValue] | None) -> dict[str, dict[str, JsonValue]]:
    """Return only object-valued scope metadata rows."""
    return {key: row for key, row in (value or {}).items() if isinstance(row, dict)}


def _axis_value(axis: JsonValue, key: str) -> JsonValue:
    """Read one value from an axis payload when it is an object."""
    return axis.get(key) if isinstance(axis, dict) else None


def _grade_market(axis: dict[str, JsonValue], brand_payloads: list[dict[str, JsonValue]], *, is_large: bool) -> tuple[str, list[str]]:
    """Assign a conservative A-D grade with explicit reasons."""
    reasons: list[str] = []
    topics = axis.get("topics")
    topic_count = len(topics) if isinstance(topics, list) else 0
    if axis.get("status") != "ok" or not brand_payloads:
        return "D", ["missing_or_quarantined_axis_or_brand"]
    if not 3 <= topic_count <= 7:
        reasons.append("topic_count_outside_3_7")
    guard_fail = any(_guard_status(payload) == "fail" for payload in brand_payloads)
    if guard_fail:
        return "D", [*reasons, "mechanical_guard_failed"]
    if is_large and competitor_separation(brand_payloads).get("status") == "weak":
        reasons.append("weak_competitor_separation")
    empty_brand_ratio = sum(1 for payload in brand_payloads if not payload.get("topic_shares")) / max(1, len(brand_payloads))
    if empty_brand_ratio > 0.5:
        reasons.append("many_brands_without_topics")
    if not reasons:
        return "A", []
    if len(reasons) == 1 and reasons[0] in {"weak_competitor_separation", "topic_count_outside_3_7"}:
        return "B", reasons
    return "C", reasons


def _guard_status(payload: dict[str, JsonValue]) -> str:
    """Read nested guard status from a brand payload."""
    qc = payload.get("qc")
    if isinstance(qc, dict):
        guard = qc.get("guard")
        if isinstance(guard, dict):
            return str(guard.get("status") or "")
    return ""


def _top_labels(value: JsonValue) -> list[str]:
    """Return up to three top labels from topic/share rows."""
    if not isinstance(value, list):
        return []
    rows = [item for item in value if isinstance(item, dict)]
    rows.sort(key=lambda item: float(item.get("share_pct") or item.get("hit_rows") or 0.0), reverse=True)
    return [str(item.get("label") or item.get("topic_label") or "") for item in rows[:3] if item.get("label") or item.get("topic_label")]


def _label_overlap(left: list[str], right: list[str]) -> float:
    """Score best lexical overlap across top labels."""
    if not left or not right:
        return 0.0
    scores = []
    for label in left:
        left_tokens = set(label.replace("/", " ").split())
        scores.append(max((len(left_tokens & set(candidate.replace("/", " ").split())) / max(1, len(left_tokens | set(candidate.replace("/", " ").split()))) for candidate in right), default=0.0))
    return round(sum(scores) / len(scores), 3)


def _vector_distance(left: dict[str, JsonValue], right: dict[str, JsonValue]) -> float:
    """Calculate Manhattan distance between two topic-share vectors."""
    left_map = share_map(left)
    right_map = share_map(right)
    topic_ids = set(left_map) | set(right_map)
    return sum(abs(left_map.get(topic_id, 0.0) - right_map.get(topic_id, 0.0)) for topic_id in topic_ids)
