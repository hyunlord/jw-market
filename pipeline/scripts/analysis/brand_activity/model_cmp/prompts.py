from __future__ import annotations

import json

from .models import JsonValue, KeywordRow, TopicDefinition


PROMPT_VERSION = "model_cmp_v2"


def market_axis_prompt(*, scope_id: str, scope_label: str, rows: list[KeywordRow], seed_dictionary: JsonValue) -> list[dict[str, str]]:
    """Create the market/group common-axis prompt for GenOS."""
    payload: dict[str, JsonValue] = {
        "scope_id": scope_id,
        "scope_label": scope_label,
        "seed_dictionary": seed_dictionary,
        "rows": [_row_for_prompt(row) for row in rows],
        "output_schema": {
            "scope_id": scope_id,
            "axis_version": "string",
            "topics": [{"topic_id": "T1", "label": "한국어", "definition": "1문장", "keywords": ["대표어"]}],
            "etc": {"label": "기타", "definition": "행사/안내성 또는 축 외 메시지"},
        },
    }
    return [
        {
            "role": "system",
            "content": (
                "너는 JW중외제약 MI팀의 브랜드 활동 메시지 분석가다. "
                "시장 또는 시장군 단위 공통 토픽 축을 5~8개 도출한다. "
                "브랜드 비교 매트릭스에 재사용 가능한 축만 만들고 원문 문장을 인용하지 않으며 JSON 객체만 반환한다. "
                "반드시 최상위 키는 scope_id, axis_version, topics, etc만 사용하고 topics는 배열이어야 한다."
            ),
        },
        {"role": "user", "content": "아래 입력으로 공통 토픽 축을 생성하라.\n" + json.dumps(payload, ensure_ascii=False, sort_keys=True)},
    ]


def brand_share_prompt(
    *,
    scope_id: str,
    brand: str,
    axis_version: str,
    topics: list[TopicDefinition],
    rows: list[KeywordRow],
) -> list[dict[str, str]]:
    """Create the brand-share prompt using a fixed market/common axis."""
    payload: dict[str, JsonValue] = {
        "scope_id": scope_id,
        "brand": brand,
        "axis_version": axis_version,
        "topics": [
            {"topic_id": topic.topic_id, "label": topic.label, "definition": topic.definition, "keywords": list(topic.keywords)}
            for topic in topics
        ],
        "rows": [_row_for_prompt(row) for row in rows],
        "output_schema": {
            "brand": brand,
            "scope_id": scope_id,
            "axis_version": axis_version,
            "denominator": "brand_row_count_primary_topic",
            "topic_shares": [{"topic_id": "T1", "label": "한국어", "share_pct": 0.0, "row_count": 0}],
            "etc_pct": 0.0,
            "cross_insights": {"evolution": "increase", "interest": "VERY/SOMEWHAT USEFUL", "promotional": "promotional_lit YES"},
            "evidence_note": "표본 한계와 판단 기준",
        },
    }
    return [
        {
            "role": "system",
            "content": (
                "너는 JW중외제약 MI팀의 브랜드 활동 메시지 분석가다. "
                "주어진 공통 토픽 축에 브랜드 메시지를 주토픽 기준으로 배분한다. "
                "기타 포함 합계 100%로 맞추고 원문 문장을 인용하지 않으며 행 참조만 사용할 수 있고 JSON 객체만 반환한다."
                "반드시 최상위 키는 brand, scope_id, axis_version, denominator, topic_shares, etc_pct, cross_insights, evidence_note만 사용하고 topic_shares는 배열이어야 한다."
            ),
        },
        {"role": "user", "content": "아래 입력으로 브랜드 토픽 비율을 생성하라.\n" + json.dumps(payload, ensure_ascii=False, sort_keys=True)},
    ]


def prompt_template_manifest() -> dict[str, JsonValue]:
    """Return prompt-version metadata without actual row text."""
    return {
        "prompt_version": PROMPT_VERSION,
        "temperature": 0.0,
        "denominator": "brand_row_count_primary_topic",
        "audit_policy": "Prompt templates are stored; prompts with raw keyword_text are not dumped.",
    }


def _row_for_prompt(row: KeywordRow) -> dict[str, JsonValue]:
    """Convert a source row into the transient GenOS prompt shape."""
    return {
        "row_ref": f"keyword:{row.row_id}",
        "period_ym": row.period_ym,
        "atc4": row.atc4,
        "brand": row.brand,
        "text": row.keyword_text,
        "interest": row.interest,
        "prescription_frequency": row.prescription_frequency,
        "prescription_evolution": row.prescription_evolution,
        "promotional_lit": row.promotional_lit,
        "abstract_lit": row.abstract_lit,
        "patient_lit": row.patient_lit,
        "specialty": row.specialty,
        "visit_location": row.visit_location,
    }
