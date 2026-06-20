from __future__ import annotations

from .models import JsonValue, TopicDefinition
from .quality import dictionary_cross_check, drift_check, mechanical_guard


def artificial_qc_probe(axis_topics: dict[str, list[TopicDefinition]]) -> dict[str, JsonValue]:
    """Run the three QC layers against intentionally broken payloads."""
    first_market = next(iter(axis_topics), "UNKNOWN")
    valid_ids = {topic.topic_id for topic in axis_topics.get(first_market, [TopicDefinition("T1", "fallback", "", ())])}
    bad_share = {"status": "ok", "topic_shares": [{"topic_id": "HAL", "label": "환각", "share_pct": 70.0}], "etc_pct": 40.0}
    drift_current = {"topic_shares": [{"topic_id": "T1", "share_pct": 90.0}], "etc_pct": 10.0}
    drift_previous = {"topic_shares": [{"topic_id": "T1", "share_pct": 20.0}], "etc_pct": 80.0}
    dict_payload = {"topics": [{"label": "완전히 다른 축", "share_pct": 90.0}]}
    return {
        "mechanical_guard": mechanical_guard(bad_share, valid_topic_ids=valid_ids),
        "drift": drift_check(drift_current, drift_previous, threshold_pp=20.0),
        "dict_xcheck": dictionary_cross_check({"topic_shares": [{"label": "LLM 상위", "share_pct": 90.0}]}, dict_payload),
    }
