from __future__ import annotations

import pytest

from jw_chat_agent_poc.orchestrator.claim_policy import (
    FORBIDDEN_BY_FACT_TYPE,
    apply_claim_policy,
    claim_policy_report,
)


CHANNEL_FACT_MD = "\n".join(
    [
        "### 필수 답변 fact",
        "| 구분 | 반드시 반영할 내용 |",
        "| --- | --- |",
        "| channel 상위 | 1위 의원 시장점유율 3.37% 매출 41.93억원 |",
        "| channel 상위 | 2위 종합병원 시장점유율 4.22% 매출 20.57억원 |",
        "| channel 상위 | 3위 상급종합병원 시장점유율 4.49% 매출 17.64억원 |",
    ]
)

CLINICAL_REGISTRY_FACT_MD = "\n".join(
    [
        "- pitavastatin: 글로벌 임상시험 = NCT00257686 · Study to Compare the Efficacy and Safety of Pitavastatin and Pravastatin in Elderly Patients · https://clinicaltrials.gov/study/NCT00257686 [ClinicalTrials.gov 임상시험 정보]",
        "- 고지혈증 (20120928): 국내 임상시험 = YH14700 [식약처 의약품 정보]",
    ]
)


def test_clinical_registry_policy_removes_outcome_and_market_elevation_but_keeps_evidence() -> None:
    answer = "\n".join(
        [
            "리바로는 폭넓은 환자군에서 임상적 근거를 확보했고 장기적인 혈관 보호 효과를 입증했습니다.",
            "이 결과는 안전성 프로파일을 강화하고 향후 시장 선점 경쟁을 주도할 가능성을 시사합니다.",
            "글로벌 임상시험을 통해 안전성을 확보하고 약제 특성을 검증해 왔으며 임상적 유용성을 확인했습니다.",
            "경동맥 연구는 혈관 건강 개선 가능성과 적응증 확대를 보여주며 신뢰할 수 있는 치료 옵션임을 시사합니다.",
            "고지혈증 치료제는 복합 처방 효율성을 극대화하는 방향으로 진화하고 있습니다.",
            "| 임상시험 번호 | 연구 제목 |",
            "| --- | --- |",
            "| NCT00257686 | Study to Compare the Efficacy and Safety of Pitavastatin and Pravastatin in Elderly Patients |",
        ]
    )

    revised = apply_claim_policy("리바로 임상시험", answer, CLINICAL_REGISTRY_FACT_MD)

    for forbidden in (
        "임상적 근거",
        "입증",
        "혈관 보호 효과",
        "안전성 프로파일",
        "시장 선점",
        "가능성을 시사",
        "안전성을 확보",
        "약제 특성을 검증",
        "임상적 유용성을 확인",
        "혈관 건강 개선 가능성",
        "적응증 확대",
        "신뢰할 수 있는 치료 옵션",
        "효율성을 극대화",
        "방향으로 진화",
    ):
        assert forbidden not in revised
    assert "ClinicalTrials.gov 등록정보에서 글로벌 임상시험 1건" in revised
    assert "식약처 등록정보에서 국내 임상시험 1건" in revised
    assert "연구 등록과 제목을 보여주는 근거" in revised
    assert "결과·효과·안전성 확정이나 개발 성공을 뜻하지는 않습니다" in revised
    assert "NCT00257686" in revised
    assert "Study to Compare the Efficacy and Safety" in revised

    report = claim_policy_report(revised, CLINICAL_REGISTRY_FACT_MD)
    assert "external_clinical_registry" in report["active_fact_types"]
    assert report["forbidden_claims_remaining"] == ()


@pytest.mark.parametrize(
    "forbidden",
    [
        "임상적 근거",
        "입증",
        "전이",
        "낙수효과",
        "Cash Cow",
        "표준 치료제",
    ],
)
def test_channel_policy_removes_forbidden_claims_and_inserts_safe_summary(forbidden: str) -> None:
    answer = (
        f"리바로는 의원 매출 41.93억원이 {forbidden}를 보여줍니다. "
        "상급종합병원 점유율은 처방 로열티가 높기 때문입니다."
    )

    revised = apply_claim_policy("리바로 채널별 매출", answer, CHANNEL_FACT_MD)

    assert forbidden not in revised
    assert "로열티" not in revised
    assert "리바로 채널별 매출은 의원 41.93억원, 종합병원 20.57억원, 상급종합병원 17.64억원 순입니다" in revised
    assert "시장점유율은 상급종합병원 4.49%, 종합병원 4.22%, 의원 3.37% 순입니다" in revised
    assert "매출 볼륨은 의원" in revised
    assert "상대 점유율 우위는 상급종합병원" in revised
    assert "현재 데이터만으로 확인할 수 없" in revised


def test_channel_policy_leaves_allowed_observation_unchanged() -> None:
    answer = "리바로는 의원 매출 41.93억원으로 볼륨이 가장 크고, 상급종합병원 시장점유율 4.49%가 가장 높습니다."

    revised = apply_claim_policy("리바로 채널별 매출", answer, CHANNEL_FACT_MD)

    assert "리바로 채널별 매출은 의원 41.93억원, 종합병원 20.57억원, 상급종합병원 17.64억원 순입니다" in revised
    assert "| 채널 | 시장점유율 | 매출 |" in revised
    assert "| 의원 | 3.37% | 41.93억원 |" in revised


def test_channel_policy_filters_markdown_headings_and_bullets() -> None:
    answer = "\n".join(
        [
            "### 채널별 근거 기반 인과 분석",
            "* **의원 채널:** 의원 채널은 캐시카우 역할을 수행합니다.",
            "* **대형 병원:** 임상적 근거와 처방 전이 효과를 기대할 수 있습니다.",
            "| 채널 | 시장점유율 | 매출 |",
            "| --- | --- | --- |",
            "| 의원 | 3.37% | 41.93억원 |",
        ]
    )

    revised = apply_claim_policy("리바로 채널별 매출", answer, CHANNEL_FACT_MD)

    assert "인과 분석" not in revised
    assert "캐시카우" not in revised
    assert "임상적 근거" not in revised
    assert "처방 전이" not in revised
    assert "| 의원 | 3.37% | 41.93억원 |" in revised
    assert "현재 데이터만으로 확인할 수 없" in revised


def test_channel_policy_normalizes_adjacent_unsupported_interpretation() -> None:
    answer = (
        "상급종합병원에서의 높은 MS는 리바로의 브랜드 신뢰도를 상징합니다. "
        "의원 채널에서 점유율을 끌어올릴 경우 폭발적인 매출 증대가 가능할 것으로 판단됩니다."
    )

    revised = apply_claim_policy("리바로 채널별 매출", answer, CHANNEL_FACT_MD)

    assert "브랜드 신뢰도" not in revised
    assert "폭발적인 매출 증대" not in revised
    assert "현재 데이터만으로 확인할 수 없" in revised
    assert "| 상급종합병원 | 4.49% | 17.64억원 |" in revised


def test_channel_policy_uses_rendered_channel_table_when_fact_md_lacks_channel_rows() -> None:
    answer = "\n".join(
        [
            "| 채널 | 시장점유율 | 매출 |",
            "| --- | --- | --- |",
            "| 의원 | 3.37% | 41.93억원 |",
            "| 종합병원 | 4.22% | 20.60억원 |",
            "| 상급종합병원 | 4.49% | 17.56억원 |",
            "## 처리 시간",
            "- 총 소요: 33.69초",
        ]
    )

    revised = apply_claim_policy("리바로 채널별 매출", answer, "")

    assert "리바로 채널별 매출은 의원 41.93억원, 종합병원 20.60억원, 상급종합병원 17.56억원 순입니다" in revised
    assert "시장점유율은 상급종합병원 4.49%, 종합병원 4.22%, 의원 3.37% 순입니다" in revised
    assert "현재 데이터만으로 확인할 수 없" in revised
    assert "## 처리 시간" in revised


def test_channel_policy_uses_compacted_rendered_channel_table() -> None:
    answer = (
        "| 채널 | 시장점유율 | 매출 || --- | --- | --- || 의원 | 3.37% | 41.93억원 || "
        "종합병원 | 4.22% | 20.60억원 || 상급종합병원 | 4.49% | 17.56억원 |"
    )

    revised = apply_claim_policy("리바로 채널별 매출", answer, "")

    assert "리바로 채널별 매출은 의원 41.93억원, 종합병원 20.60억원, 상급종합병원 17.56억원 순입니다" in revised
    assert "현재 데이터만으로 확인할 수 없" in revised
    assert "| 의원 | 3.37% | 41.93억원 |" in revised


def test_channel_policy_falls_back_to_rendered_table_when_fact_md_marks_channel_without_rows() -> None:
    fact_md = "### 리바로 channel별 근거\nchannel 상위 데이터가 있으며 시장점유율과 매출을 포함합니다.\n"
    answer = (
        "| 채널 | 시장점유율 | 매출 || --- | --- | --- || 의원 | 3.37% | 41.93억원 || "
        "종합병원 | 4.22% | 20.60억원 || 상급종합병원 | 4.49% | 17.56억원 |"
    )

    revised = apply_claim_policy("리바로 채널별 매출", answer, fact_md)

    assert "리바로 채널별 매출은 의원 41.93억원, 종합병원 20.60억원, 상급종합병원 17.56억원 순입니다" in revised
    assert "현재 데이터만으로 확인할 수 없" in revised
    assert "| 의원 | 3.37% | 41.93억원 |" in revised


def test_channel_policy_uses_wrapped_rendered_channel_table_cells() -> None:
    answer = """| 채널 | 시장점유율 | 매출 |
| ---

| --- | --- |
| 의원 | 3.37%

| 41.93억원 |
| 종합병원 | 4.22%

| 20.60억원 |
| 상급종합병원 | 4.49%

| 17.56억원 |"""

    revised = apply_claim_policy("리바로 채널별 매출", answer, "")

    assert "리바로 채널별 매출은 의원 41.93억원, 종합병원 20.60억원, 상급종합병원 17.56억원 순입니다" in revised
    assert "현재 데이터만으로 확인할 수 없" in revised
    assert "| 의원 | 3.37% | 41.93억원 |" in revised


def test_claim_policy_is_table_driven_for_channel_cross_section() -> None:
    assert "channel_cross_section" in FORBIDDEN_BY_FACT_TYPE
    assert "causal_analysis_unverified" in FORBIDDEN_BY_FACT_TYPE["channel_cross_section"]
    assert "clinical_evidence" in FORBIDDEN_BY_FACT_TYPE["channel_cross_section"]
    assert "cash_cow_unverified" in FORBIDDEN_BY_FACT_TYPE["channel_cross_section"]


def test_claim_policy_registers_competitive_and_news_fact_types_without_new_branches() -> None:
    assert FORBIDDEN_BY_FACT_TYPE["brand_share_delta"] == (
        "direct_switching",
        "cannibalization",
        "absorption_replacement",
        "causal_competition_win",
    )
    assert FORBIDDEN_BY_FACT_TYPE["news_context"] == (
        "quantified_sales_impact",
        "causal_market_impact_without_metric",
        "news_claim_elevation",
    )


def test_brand_share_delta_policy_preserves_current_cautious_q05_language() -> None:
    fact_md = "\n".join(
        [
            "### 필수 답변 fact",
            "| 항목 | 값 |",
            "| --- | --- |",
            "| 브랜드 MS 변화 | 0.53%p |",
            "| 비교 브랜드 MS 변화 | -0.56%p |",
        ]
    )
    answer = (
        "리바로젯 0.53%p와 리피토 -0.56%p로 반대 방향입니다. "
        "집계 데이터만으로 직접 처방 이동은 확인할 수 없습니다. "
        "따라서 복합제 중심 재편 후보 신호로 해석됩니다."
    )

    revised = apply_claim_policy("리바로 경쟁 구도 변화는 어때", answer, fact_md)

    assert revised == answer


def test_brand_share_delta_policy_blocks_clear_direct_switching_claim() -> None:
    fact_md = "| 브랜드 MS 변화 | 0.53%p |\n| 비교 브랜드 MS 변화 | -0.56%p |"
    answer = (
        "리피토에서 리바로젯으로 직접 처방 이동이 발생했습니다. "
        "점유율은 서로 반대 방향으로 움직였습니다."
    )

    revised = apply_claim_policy("리바로 경쟁 구도 변화는 어때", answer, fact_md)

    assert "직접 처방 이동이 발생" not in revised
    assert "점유율은 서로 반대 방향으로 움직였습니다" in revised


def test_news_context_policy_preserves_current_cautious_q01_language() -> None:
    fact_md = "### 인사이트 근거 fact - 뉴스/이슈\n| 날짜 | 제목 | 출처 | URL | 요약 | 매칭 발췌 |"
    answer = (
        "최근 이슈는 시장 동향의 전조가 될 수 있으므로 모니터링이 필요합니다. "
        "다만 개별 환자 처방 변경 사유나 경쟁사 프로모션은 포함하지 않습니다."
    )

    revised = apply_claim_policy("리바로 관련 최근 이슈", answer, fact_md)

    assert revised == answer


def test_news_context_policy_blocks_quantified_news_sales_impact_claim() -> None:
    fact_md = "### 인사이트 근거 fact - 뉴스/이슈\n| 날짜 | 제목 | 출처 | URL | 요약 | 매칭 발췌 |"
    answer = (
        "이번 뉴스 때문에 리바로 매출이 12억원 감소했습니다. "
        "기사 제목과 요약은 시장 동향 참고 자료입니다."
    )

    revised = apply_claim_policy("리바로 관련 최근 이슈", answer, fact_md)

    assert "12억원 감소" not in revised
    assert "기사 제목과 요약은 시장 동향 참고 자료입니다" in revised


def test_news_context_policy_blocks_claim_elevation_language() -> None:
    fact_md = "### 인사이트 근거 fact - 뉴스/이슈\n| 날짜 | 제목 | 출처 | URL | 요약 | 매칭 발췌 |"
    answer = (
        "뉴스로 리바로의 시장 확대가 입증되었습니다. "
        "기사 제목과 요약은 시장 동향 참고 자료입니다."
    )

    revised = apply_claim_policy("리바로 관련 최근 뉴스", answer, fact_md)

    assert "입증" not in revised
    assert "기사 제목과 요약은 시장 동향 참고 자료입니다" in revised
