from __future__ import annotations

from scripts.bq_screen.models import BqCase, BqScreenInput
from scripts.bq_screen.rules import screen_answer


def _input(
    text: str,
    *,
    case_id: str = "CASE",
    brand: str = "리바로",
    case_type: str = "A1",
    question: str = "리바로 매출 알려줘",
    cohort: str = "",
    tools: tuple[str, ...] = (),
    status: int = 200,
    elapsed_s: float = 10.0,
    error: str | None = None,
) -> BqScreenInput:
    return BqScreenInput(
        case=BqCase(id=case_id, brand=brand, type=case_type, question=question, cohort=cohort),
        status=status,
        elapsed_s=elapsed_s,
        error=error,
        text=text,
        tools=tools,
    )


def _flags(result) -> set[str]:
    return set(result.flags) | set(result.confirm_needed)


def test_detects_same_entity_metric_conflict_and_failed_zero_surface() -> None:
    text = (
        "악템라는 2025-Q3→2025-Q4 기준 시장점유율 0.00%, 순위 23/26입니다.\n"
        "2025-Q4에는 48.19억원(MS 4.34%)을 기록했습니다.\n"
        "- 브랜드 핵심 지표: 악템라 2026-04 매출 0.00억원 순위 23/26\n"
    )
    result = screen_answer(
        _input(text, brand="악템라", case_type="A1 규모/추이", question="악템라 매출 추이", cohort="actemra_6_dual_class"),
    )

    assert "same_entity_period_metric_conflict" in result.flags
    assert "query_failed_value_surface" in result.flags
    assert "market_structure_split_missing" in result.confirm_needed


def test_query_failed_rule_does_not_fire_after_block_message_without_value_surface() -> None:
    text = (
        "2026-04 값은 조회 실패로 표시하지 않습니다.\n"
        "악템라는 2025-Q4 48.19억원(MS 4.34%)입니다.\n"
        "Class 1/Class 2 split 기준으로 해석합니다.\n"
    )
    result = screen_answer(_input(text, brand="악템라", cohort="actemra_6_dual_class"))

    assert "query_failed_value_surface" not in result.flags
    assert "same_entity_period_metric_conflict" not in result.flags
    assert "market_structure_split_missing" not in result.confirm_needed


def test_detects_requested_source_mismatch() -> None:
    text = (
        "Cortellis 기준 파이프라인 현황입니다.\n"
        "## 출처\n"
        "- 외부: ClinicalTrials/MFDS 임상 정보\n"
    )
    result = screen_answer(
        _input(
            text,
            case_id="R_T4_cortellis",
            question="Cortellis 기준 이상지질혈증 파이프라인과 리바로 경쟁 임상 현황을 분석해줘",
        ),
    )

    assert "requested_vs_actual_source_mismatch" in result.flags


def test_requested_source_unavailable_with_alternate_reference_is_not_mismatch() -> None:
    text = (
        "Cortellis 데이터는 현재 운영 데이터에 미보유입니다.\n\n"
        "### 대체 참고\n"
        "- ClinicalTrials/MFDS 결과는 Cortellis 데이터가 아니므로 요청 소스 기준 결론으로 승격하지 않습니다.\n\n"
        "## 출처\n"
        "- 외부 API: ClinicalTrials/MFDS 임상 정보\n"
    )
    result = screen_answer(
        _input(
            text,
            case_id="R_T4_cortellis_fixed",
            question="Cortellis 기준 이상지질혈증 파이프라인과 리바로 경쟁 임상 현황을 분석해줘",
        ),
    )

    assert "requested_vs_actual_source_mismatch" not in result.flags


def test_detects_positioning_intent_without_axis() -> None:
    text = (
        "- 인사이트: 리바로젯 점유 0.53%p 상승\n"
        "### 상위 브랜드 추이\n"
        "| 브랜드 | 최신 MS |\n| --- | --- |\n| 리바로 | 3.76% |\n"
    )
    result = screen_answer(
        _input(
            text,
            case_type="B2 포지셔닝",
            question="리바로의 경쟁 제품 대비 포지셔닝과 차별점을 시장 데이터 기준으로 설명해줘",
        ),
    )

    assert "intent_required_axis_missing" in result.confirm_needed


def test_detects_news_relevance_grade_missing() -> None:
    text = (
        "External/Internal 분석입니다.\n"
        "| 구분 | 요인 | 영향방향 |\n| --- | --- | --- |\n| External | 바이오시밀러 뉴스 | 위협 |\n"
        "메디칼타임즈 [기사](https://example.test/news) — 실적 증가\n"
    )
    result = screen_answer(
        _input(
            text,
            brand="악템라",
            case_type="E2 인과",
            question="[악템라] 목표 시장에서의 향후 예상되는 시장 변화 요인이 있는가? External/Internal로 나눠 실제 뉴스 근거와 함께 분석해줘",
        ),
    )

    assert "news_relevance_grade_missing" in result.confirm_needed


def test_web_contamination_is_gated_by_web_search_tool() -> None:
    text = "본문 출처 https://example.test/news\n\n### 웹 검색 결과(미검증)\n- https://example.test/news"

    without_web = screen_answer(_input(text, tools=("deep_analysis_related_news",)))
    with_web = screen_answer(_input(text, tools=("web_search",)))

    assert "web_contamination" not in _flags(without_web)
    assert "web_contamination" in with_web.flags


def test_normal_livalo_answer_has_no_semantic_flags() -> None:
    text = (
        "리바로는 2026-04 기준 매출 84.93억원, 시장점유율 3.76%, 순위 6/470입니다.\n"
        "참고: strategy_006 기준 순위는 6/516으로 표시될 수 있음\n"
        "### 리바로 매출 시계열\n"
        "| 기간 | 매출 | MS |\n| --- | --- | --- |\n| 2026-04 | 84.93억원 | 3.76% |\n"
    )
    result = screen_answer(_input(text, case_type="A1 규모/추이"))

    semantic = {
        "same_entity_period_metric_conflict",
        "query_failed_value_surface",
        "requested_vs_actual_source_mismatch",
        "intent_required_axis_missing",
        "market_structure_split_missing",
        "news_relevance_grade_missing",
    }
    assert not (semantic & _flags(result))


def test_unavailable_without_five_step_is_flagged_but_five_step_passes() -> None:
    naked = screen_answer(_input("현재 데이터 미보유 상태라 확인 불가합니다. 추가 데이터가 필요합니다."))
    layered = screen_answer(
        _input(
            "| 단계 | 내용 |\n"
            "| 1. 미보유 데이터 | 없음 |\n"
            "| 2. 현재 가능한 proxy | UBIST 추이 |\n"
            "| 3. 해석 가능한 상한선 | 예측 아님 |\n"
            "| 4. 확인 필요 데이터 | forecast |\n"
            "| 5. 확보 시 수행할 분석 | 시계열 예측 |\n",
        ),
    )

    assert "naked_unavailable" in naked.flags
    assert "naked_unavailable" not in layered.flags
