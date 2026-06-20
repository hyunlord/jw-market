from __future__ import annotations

import json

from pipeline.scripts.analysis.brand_activity.topic_redesign.dictionary import MARKET_TEMPLATES

from .models import BrandDescription, JsonValue, KeywordRow, TopicDefinition


PROMPT_VERSION = "auto_topic_v3_singleconcept_top7"


def market_seed_dictionary(atc4: str, redesign_payload: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Return REDESIGN seed hints with MARKET_TEMPLATES fallback labels."""
    value = redesign_payload.get(atc4)
    if isinstance(value, dict) and value:
        return value
    return {
        template.label: {"keywords": list(template.keywords), "note": template.note}
        for template in MARKET_TEMPLATES.get(atc4, ())
    }


def market_axis_prompt(
    *,
    atc4: str,
    rows: list[KeywordRow],
    seed_dictionary: dict[str, JsonValue],
    scope_id: str | None = None,
    market_name: str | None = None,
    atc4_values: list[str] | None = None,
) -> list[dict[str, str]]:
    """Create the market-common topic-axis prompt for one ATC4."""
    resolved_scope_id = scope_id or f"atc4:{atc4}"
    payload: dict[str, JsonValue] = {
        "scope_id": resolved_scope_id,
        "atc4": atc4,
        "atc4_values": atc4_values or [atc4],
        "market_name": market_name or atc4,
        "window": "recent_1_year_or_available_10_months",
        "seed_dictionary": seed_dictionary,
        "rows": [_row_for_prompt(row) for row in rows],
        "output_schema": {
            "scope_id": resolved_scope_id,
            "axis_version": "string",
            "topics": [{"topic_id": "T1", "label": "짧은 한국어", "definition": "한 의미만 담은 1문장", "keywords": ["대표어"]}],
        },
    }
    return [
        {
            "role": "system",
            "content": (
                "너는 JW중외제약 MI팀의 브랜드 활동 메시지 분석가다. "
                "시장 단위 공통 토픽 축을 최대 7개까지 자동 생성한다. "
                "소규모 시장은 3~5개까지 허용하되 신뢰도 한계를 axis_note에 남긴다. "
                "각 토픽 라벨은 짧은 명사구 1개념만 담아야 하며 '및', '/', ','로 두 개념을 합치지 않는다. "
                "두 개념이 모두 중요하면 별도 토픽으로 분리하고, 7개를 넘으면 우선순위 낮은 개념을 버린다. "
                "유사/동의어 토픽은 병합한다. "
                "라벨은 브랜드 비교 매트릭스에 재사용 가능한 비즈니스 단위여야 한다. "
                "원문 문장을 인용하지 말고 JSON 객체만 반환한다."
            ),
        },
        {"role": "user", "content": "다음 입력으로 시장 공통 토픽 축을 생성하라.\n" + json.dumps(payload, ensure_ascii=False, sort_keys=True)},
    ]


def market_axis_merge_prompt(
    *,
    atc4: str,
    scope_id: str,
    market_name: str,
    atc4_values: list[str],
    candidate_axes: list[dict[str, JsonValue]],
) -> list[dict[str, str]]:
    """Create a raw-text-free prompt that merges chunk candidate axes into one market axis."""
    payload: dict[str, JsonValue] = {
        "scope_id": scope_id,
        "atc4": atc4,
        "atc4_values": atc4_values,
        "market_name": market_name,
        "candidate_axes": candidate_axes,
        "output_schema": {
            "scope_id": scope_id,
            "axis_version": "string",
            "topics": [{"topic_id": "T1", "label": "짧은 한국어", "definition": "한 의미만 담은 1문장", "keywords": ["대표어"]}],
        },
    }
    return [
        {
            "role": "system",
            "content": (
                "너는 JW중외제약 MI팀의 브랜드 활동 메시지 분석가다. "
                "여러 chunk에서 나온 후보 토픽을 중복 제거해 시장 공통 토픽 축 최대 7개로 통합한다. "
                "각 토픽 라벨은 짧은 명사구 1개념만 담고 '및', '/', ','로 두 개념을 합치지 않는다. "
                "후보가 복합 라벨이면 개념을 분리하거나 우선순위 높은 한 개념만 남긴다. "
                "유사/동의어 토픽은 병합한다. "
                "브랜드 비교 매트릭스에 안정적으로 재사용 가능한 축만 남기고 JSON 객체만 반환한다."
            ),
        },
        {"role": "user", "content": "다음 후보 토픽들을 하나의 시장 공통축으로 통합하라.\n" + json.dumps(payload, ensure_ascii=False, sort_keys=True)},
    ]


def brand_share_prompt(
    *,
    atc4: str,
    brand: str,
    axis_version: str,
    topics: list[TopicDefinition],
    description: BrandDescription,
    rows: list[KeywordRow],
    scope_id: str | None = None,
    market_name: str | None = None,
    atc4_values: list[str] | None = None,
) -> list[dict[str, str]]:
    """Create the brand-share prompt against a fixed market axis."""
    resolved_scope_id = scope_id or f"atc4:{atc4}"
    payload: dict[str, JsonValue] = {
        "scope_id": resolved_scope_id,
        "atc4": atc4,
        "atc4_values": atc4_values or [atc4],
        "market_name": market_name or atc4,
        "brand": brand,
        "brand_description": {
            "kr_canonical": description.kr_canonical,
            "is_jw": description.is_jw,
            "molecule": list(description.molecule),
            "manufacturer": list(description.manufacturer),
            "representing_company": list(description.representing_company),
        },
        "axis_version": axis_version,
        "topics": [_topic_for_prompt(topic) for topic in topics],
        "rows": [_row_for_prompt(row) for row in rows],
        "output_schema": {
            "brand": brand,
            "atc4": atc4,
            "axis_version": axis_version,
            "denominator": "brand_row_count_primary_topic",
            "topic_shares": [{"topic_id": "T1", "label": "한국어", "share_pct": 0.0, "row_count": 0}],
            "brand_specific_topics": [{"topic_id": "B1", "label": "브랜드 특화 한국어", "definition": "시장축 밖 브랜드 대표 의미", "share_pct": 0.0, "row_count": 0}],
            "cross_insights": {"evolution": [], "interest": [], "promotional": []},
            "evidence_note": "원문 인용 없이 표본 한계와 판단 기준만 설명",
        },
    }
    return [
        {
            "role": "system",
            "content": (
                "너는 JW중외제약 MI팀의 브랜드 활동 메시지 분석가다. "
                "각 메시지를 주토픽 1개 기준으로 주어진 시장 축에 배분한다. "
                "시장 축에는 없는 브랜드 대표 의미가 있으면 brand_specific_topics에 최대 2개만 추가한다. "
                "topic_shares와 brand_specific_topics의 label은 짧은 명사구 1개념만 담고 '및', '/', ','를 쓰지 않는다. "
                "brand_specific_topics 2개는 서로 명확히 다른 개념이어야 하며, 표현만 다른 근접중복이면 1개로 병합한다. "
                "시장 축과 의미가 겹치는 개념은 brand_specific_topics로 빼지 말고 해당 시장 topic_id로 분류한다. "
                "기타/other/etc 항목은 출력하지 않는다. 시스템이 100에서 토픽 합을 빼서 사후 계산한다. "
                "시장 topic_shares에는 축에 없는 topic_id를 만들지 말고, "
                "원문 문장을 인용하지 말며 JSON 객체만 반환한다."
            ),
        },
        {"role": "user", "content": "다음 입력으로 브랜드 토픽 비율을 생성하라.\n" + json.dumps(payload, ensure_ascii=False, sort_keys=True)},
    ]


def prompt_template_manifest() -> dict[str, JsonValue]:
    """Return prompt policy metadata without any filled prompt or source text."""
    return {
        "prompt_version": PROMPT_VERSION,
        "temperature": 0.0,
        "axis_contract": "market axis up to 7 concise single-meaning topics; small markets may return 3-5 with low-confidence note",
        "share_denominator": "brand_row_count_primary_topic; etc is computed post-parse as 100 minus market and brand-specific shares",
        "label_policy": {
            "single_concept_required": True,
            "forbidden_connectors": ["및", "/", ","],
            "brand_specific_max_topics": 2,
            "brand_specific_near_duplicate_allowed": False,
        },
        "audit_policy": "Filled prompts with raw keyword_text are never persisted.",
        "scale_policy": "Large axes use chunk candidate extraction plus raw-text-free merge; large brand shares use batched primary-topic counts and up to two brand-specific topics.",
    }


def _row_for_prompt(row: KeywordRow) -> dict[str, JsonValue]:
    """Convert a Keyword row into transient prompt JSON."""
    return {
        "row_ref": f"keyword:{row.row_id}",
        "period_ym": row.period_ym,
        "atc4": row.atc4,
        "brand": row.brand,
        "message": row.keyword_text,
        "interest": row.interest,
        "prescription_frequency": row.prescription_frequency,
        "prescription_evolution": row.prescription_evolution,
        "promotional_lit": row.promotional_lit,
        "abstract_lit": row.abstract_lit,
        "patient_lit": row.patient_lit,
        "specialty": row.specialty,
        "visit_location": row.visit_location,
    }


def _topic_for_prompt(topic: TopicDefinition) -> dict[str, JsonValue]:
    """Convert a topic definition into prompt JSON."""
    return {"topic_id": topic.topic_id, "label": topic.label, "definition": topic.definition, "keywords": list(topic.keywords)}
