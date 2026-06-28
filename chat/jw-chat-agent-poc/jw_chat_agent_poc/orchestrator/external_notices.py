from __future__ import annotations

from jw_chat_agent_poc.tools.external import ExternalCall


def external_unavailable_for_missing_molecule(resolution) -> ExternalCall:
    return ExternalCall(
        tool="external_api_unavailable",
        source="external_api",
        status="unsupported",
        summary_text=(
            f"{resolution.canonical_brand}은 운영 cache_brands에서 인식됐지만 fixture sidecar에 "
            "영문 성분 정보가 없어 임상/라벨/특허 외부 API 조회를 제한했습니다."
        ),
        render_data={
            "brand": resolution.canonical_brand,
            "reason": "missing_molecule_en_sidecar",
            "market_id": resolution.market_id,
        },
    )


def seeded_false_positive_notice(resolution) -> ExternalCall:
    return ExternalCall(
        tool="clinical_false_positive_filter_notice",
        source="external_api",
        status="policy",
        summary_text=(
            f"{resolution.canonical_brand}은 검증서의 가양성 시드에 포함되어, "
            "ClinicalTrials 상위 intervention에 영문 성분이 직접 언급되는 결과만 관련 결과로 봅니다."
        ),
        render_data={
            "brand": resolution.canonical_brand,
            "molecule_en": resolution.molecule_en,
            "filter": "top_intervention_must_mention_molecule",
        },
    )
