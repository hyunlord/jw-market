from __future__ import annotations

import pytest

from jw_chat_agent_poc.orchestrator.claim_policy import (
    FORBIDDEN_BY_FACT_TYPE,
    apply_claim_policy,
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

    assert apply_claim_policy("리바로 채널별 매출", answer, CHANNEL_FACT_MD) == answer


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


def test_claim_policy_is_table_driven_for_channel_cross_section() -> None:
    assert "channel_cross_section" in FORBIDDEN_BY_FACT_TYPE
    assert "causal_analysis_unverified" in FORBIDDEN_BY_FACT_TYPE["channel_cross_section"]
    assert "clinical_evidence" in FORBIDDEN_BY_FACT_TYPE["channel_cross_section"]
    assert "cash_cow_unverified" in FORBIDDEN_BY_FACT_TYPE["channel_cross_section"]
