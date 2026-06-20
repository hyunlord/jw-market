from __future__ import annotations

import json

from pipeline.scripts.analysis.brand_activity.llm_topic.cache import build_cache_key, stable_input_hash
from pipeline.scripts.analysis.brand_activity.llm_topic.models import KeywordRow, TopicShare
from pipeline.scripts.analysis.brand_activity.llm_topic.privacy import redacted_rows_for_audit
from pipeline.scripts.analysis.brand_activity.llm_topic.response import normalized_share_payload


def sample_row(row_id: int, text: str) -> KeywordRow:
    return KeywordRow(
        row_id=row_id,
        period_ym="2025-10",
        atc4="C10C0",
        brand="LIVALOZET",
        keyword_text=text,
        interest="VERY USEFUL",
        prescription_frequency="HIGH",
        prescription_evolution="increase",
        promotional_lit="YES",
        abstract_lit="NO",
        patient_lit="NO",
        specialty="IM",
        visit_location="clinic",
        stage_row_sha256=f"hash-{row_id}",
    )


def test_redacted_audit_rows_exclude_raw_keyword_text() -> None:
    raw_text = "LDL-C 강하와 당뇨 안전성을 강조한 원문 메시지"
    payload = redacted_rows_for_audit([sample_row(1, raw_text)])
    dumped = json.dumps(payload, ensure_ascii=False)

    assert raw_text not in dumped
    assert payload[0]["text_sha256"]
    assert payload[0]["text_length"] == len(raw_text)
    assert payload[0]["stage_row_sha256"] == "hash-1"


def test_input_hash_is_stable_and_prompt_version_sensitive() -> None:
    rows = [sample_row(1, "첫 메시지"), sample_row(2, "둘째 메시지")]

    first = stable_input_hash(rows, prompt_version="llm_topic_v1", axis_version="axis_C10C0_v1")
    second = stable_input_hash(rows, prompt_version="llm_topic_v1", axis_version="axis_C10C0_v1")
    changed = stable_input_hash(rows, prompt_version="llm_topic_v2", axis_version="axis_C10C0_v1")

    assert first == second
    assert first != changed


def test_cache_key_includes_task_model_prompt_and_input_hash() -> None:
    key = build_cache_key(
        task="brand_share",
        model_serving_id="76",
        prompt_version="llm_topic_v1",
        input_hash="abc123",
    )

    assert key == "brand_share__serving-76__llm_topic_v1__abc123"


def test_normalized_share_payload_fills_etc_to_100_percent() -> None:
    payload = normalized_share_payload(
        brand="LIVALOZET",
        atc4="C10C0",
        axis_version="axis_C10C0_v1",
        row_count=10,
        shares=[
            TopicShare(topic_id="T1", label="LDL-C 강하", share_pct=54.0, row_count=5),
            TopicShare(topic_id="T2", label="당뇨 안전성/NODM", share_pct=31.0, row_count=3),
        ],
        evidence_note="sample",
    )

    assert payload["denominator"] == "brand_row_count_primary_topic"
    assert payload["etc_pct"] == 15.0
    assert sum(item["share_pct"] for item in payload["topic_shares"]) + payload["etc_pct"] == 100.0
