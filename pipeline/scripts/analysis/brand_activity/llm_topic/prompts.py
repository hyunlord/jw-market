from __future__ import annotations

import json
from collections.abc import Sequence

from .models import BrandDescription, JsonValue, KeywordRow, TopicDefinition


PROMPT_VERSION = "llm_topic_poc_v1"
AXIS_SCHEMA_VERSION = "market_axis_v1"
BRAND_SCHEMA_VERSION = "brand_share_v1"


def market_axis_prompt(*, atc4: str, seed_dictionary: JsonValue, rows: Sequence[KeywordRow]) -> list[dict[str, str]]:
    payload: dict[str, JsonValue] = {
        "atc4": atc4,
        "seed_dictionary": seed_dictionary,
        "rows": [_row_for_prompt(row) for row in rows],
        "output_schema": {
            "atc4": "string",
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
                "시장 단위 공통 토픽 축을 5~8개 도출하되, 브랜드별 비교 매트릭스에 재사용 가능한 축만 만든다. "
                "행사 안내성/판단불가 문구는 기타로 둔다. 반드시 JSON 객체만 반환한다."
            ),
        },
        {
            "role": "user",
            "content": (
                "아래 JSON 입력을 분석해 시장 공통 토픽 축을 만들어라. "
                "seed_dictionary는 힌트이며 그대로 복사하지 말고 메시지 맥락을 반영해 병합/정리한다. "
                "topic_id는 T1부터 순번으로 부여한다.\n"
                f"{json.dumps(payload, ensure_ascii=False, sort_keys=True)}"
            ),
        },
    ]


def brand_share_prompt(
    *,
    atc4: str,
    brand: str,
    axis_version: str,
    topics: Sequence[TopicDefinition],
    description: BrandDescription,
    rows: Sequence[KeywordRow],
) -> list[dict[str, str]]:
    payload: dict[str, JsonValue] = {
        "atc4": atc4,
        "brand": brand,
        "axis_version": axis_version,
        "brand_description": {
            "kr_canonical": description.kr_canonical,
            "molecule": list(description.molecule),
            "is_jw": description.is_jw,
            "manufacturer": list(description.manufacturer),
            "representing_company": list(description.representing_company),
        },
        "topics": [
            {"topic_id": topic.topic_id, "label": topic.label, "definition": topic.definition, "keywords": list(topic.keywords)}
            for topic in topics
        ],
        "rows": [_row_for_prompt(row) for row in rows],
        "output_schema": {
            "brand": brand,
            "atc4": atc4,
            "axis_version": axis_version,
            "denominator": "brand_row_count_primary_topic",
            "topic_shares": [{"topic_id": "T1", "label": "한국어", "share_pct": 0.0, "row_count": 0}],
            "etc_pct": 0.0,
            "cross_insights": {
                "evolution": "increase와 관련 높은 토픽",
                "interest": "VERY/SOMEWHAT USEFUL과 관련 높은 토픽",
                "promotional": "promotional_lit YES와 관련 높은 토픽",
            },
            "evidence_note": "표본 한계와 판단 기준",
        },
    }
    return [
        {
            "role": "system",
            "content": (
                "너는 JW중외제약 MI팀의 브랜드 활동 메시지 분석가다. "
                "주어진 시장 공통 토픽 축에 맞춰 브랜드 메시지를 주토픽 기준으로 배분한다. "
                "비율은 기타 포함 합계 100%여야 한다. 반드시 JSON 객체만 반환한다."
            ),
        },
        {
            "role": "user",
            "content": (
                "아래 JSON 입력을 분석해 브랜드별 토픽 비율과 보조 컬럼 연계 인사이트를 산출하라. "
                "분모는 입력 브랜드 행 수이며, 한 행은 가장 중요한 주 토픽 하나에만 귀속한다.\n"
                f"{json.dumps(payload, ensure_ascii=False, sort_keys=True)}"
            ),
        },
    ]


def prompt_template_manifest() -> dict[str, JsonValue]:
    return {
        "prompt_version": PROMPT_VERSION,
        "market_axis_schema_version": AXIS_SCHEMA_VERSION,
        "brand_share_schema_version": BRAND_SCHEMA_VERSION,
        "audit_policy": "Full prompt templates are persisted; actual row text prompts are not dumped to audit.",
        "denominator": "brand_row_count_primary_topic",
        "temperature": 0.0,
    }


def _row_for_prompt(row: KeywordRow) -> dict[str, JsonValue]:
    return {
        "row_ref": f"keyword:{row.row_id}",
        "period_ym": row.period_ym,
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
