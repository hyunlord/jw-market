from __future__ import annotations

import pytest

from jw_chat_agent_poc.orchestrator.answer_contract import (
    answer_contract_backfill_tool_calls,
    enforce_answer_contract,
    evaluate_answer_contract,
)


PAIR_FACT = """### 리바로 매출 시계열 fact
| 기간 | 매출 | MS |
| --- | --- | --- |
| 2025-11 | 80.00억원 | 3.50% |
| 2026-04 | 84.93억원 | 3.76% |

### 리바로젯 매출 시계열 fact
| 기간 | 매출 | MS |
| --- | --- | --- |
| 2025-11 | 108.09억원 | 4.79% |
| 2026-04 | 120.09억원 | 5.32% |
"""

TOP_FACT = """### 상위 브랜드 점유율 추이 fact
| 최신 순위 | 브랜드 | 시작 MS | 최신 MS | MS 변화 | 최신 매출 | 매출 변화 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 로수젯 | 2025-07 9.10% | 2026-04 9.17% | +0.07%p | 206.85억원 | +1.00억원 |
| 2 | 리피토 | 2025-07 6.95% | 2026-04 6.39% | -0.56%p | 144.22억원 | -15.00억원 |
| 3 | 리바로젯 | 2025-07 4.79% | 2026-04 5.32% | +0.53%p | 120.09억원 | +12.00억원 |
| 4 | 아토젯 | 2025-07 5.01% | 2026-04 5.16% | +0.15%p | 116.45억원 | +3.00억원 |
| 5 | 크레스토 | 2025-07 4.40% | 2026-04 4.29% | -0.11%p | 96.81억원 | -2.00억원 |
"""

TARGET_FACT = """### 리바로 목표 역산 fact
| 기간 | 리바로 매출 | 리바로 MS | 시장 규모 |
| --- | --- | --- | --- |
| 2025-07 | 84.76억원 | 3.92% | 2,161.94억원 |
| 2025-12 | 90.86억원 | 3.93% | 2,309.50억원 |
| 2026-04 | 84.93억원 | 3.76% | 2,256.77억원 |
"""

TARGET_SPLIT_FACT = """### 리바로 매출 시계열 fact
| 기간 | 매출 | MS |
| --- | --- | --- |
| 2025-07 | 84.76억원 | 3.92% |
| 2026-04 | 84.93억원 | 3.76% |

### 리바로 시장규모 시계열 fact
| 기간 | 시장규모 | YoY |
| --- | --- | --- |
| 2025-07 | 2,161.94억원 | 1.00% |
| 2026-04 | 2,256.77억원 | 2.00% |
"""

CHANNEL_FACT = """### 출처 유형 fact
| 출처 | 상세 |
| --- | --- |
| 데이터 상세 | UBIST - 기간 2026-04, 적용 필터 channel=의원 |
"""


@pytest.mark.parametrize(
    ("question", "fact_md", "expected_intent"),
    (
        ("리바로와 리바로젯 6개월 매출 비교", PAIR_FACT, "brand_compare"),
        ("상위 3개 브랜드 점유율 변화를 비교해줘", TOP_FACT, "share_delta_compare"),
        ("상위 5개 브랜드 합산 점유율", TOP_FACT, "top_n_share_sum"),
        ("이 시장의 브랜드 집중도는 어때", TOP_FACT, "concentration"),
        ("리바로 점유율 4% 달성에 필요한 매출", TARGET_FACT, "target_share_gap"),
        ("의원 채널에서 리바로 매출", CHANNEL_FACT, "channel_provenance"),
    ),
)
def test_completeness_intent_is_detected(question: str, fact_md: str, expected_intent: str) -> None:
    result = evaluate_answer_contract(question, "미완성 답변", {"fact_md": fact_md})
    assert result["intent"] == expected_intent


def test_brand_compare_repairs_both_brand_series() -> None:
    revised = enforce_answer_contract("리바로와 리바로젯 6개월 매출 비교", "리바로젯은 증가했습니다.", {"fact_md": PAIR_FACT})
    assert "리바로 | 2025-11 | 80.00억원 | 2026-04 | 84.93억원 | +4.93억원 | +6.16%" in revised
    assert "리바로젯 | 2025-11 | 108.09억원 | 2026-04 | 120.09억원 | +12.00억원 | +11.10%" in revised


def test_share_delta_compare_repairs_each_requested_brand() -> None:
    revised = enforce_answer_contract("상위 3개 브랜드 점유율 변화를 비교해줘", "로수젯은 상승했습니다.", {"fact_md": TOP_FACT})
    assert "로수젯 | 2025-07 9.10% | 2026-04 9.17% | +0.07%p | 상승" in revised
    assert "리피토 | 2025-07 6.95% | 2026-04 6.39% | -0.56%p | 하락" in revised
    assert "리바로젯 | 2025-07 4.79% | 2026-04 5.32% | +0.53%p | 상승" in revised


def test_top_n_share_sum_puts_exact_sum_first() -> None:
    revised = enforce_answer_contract("상위 5개 브랜드 합산 점유율", "개별 점유율은 표와 같습니다.", {"fact_md": TOP_FACT})
    assert revised.startswith("상위 5개 합계 시장점유율은 30.33%입니다.")
    assert revised.count("% |") >= 5


def test_concentration_adds_qualitative_conclusion_and_cr_values() -> None:
    revised = enforce_answer_contract("이 시장의 브랜드 집중도는 어때", "상위 브랜드가 있습니다.", {"fact_md": TOP_FACT})
    assert "분산" in revised
    assert "CR3 20.88%" in revised
    assert "CR5 30.33%" in revised


def test_concentration_prefers_hhi_metric_fact_without_top_share_rows() -> None:
    fact_md = """### 리바로 지표 fact
| 항목 | 값 |
| --- | --- |
| HHI | 842.50 |
"""
    revised = enforce_answer_contract("리바로 시장의 브랜드 집중도는 어때", "시장 지표입니다.", {"fact_md": fact_md})
    assert "분산" in revised
    assert "HHI 842.50" in revised
    assert evaluate_answer_contract("리바로 시장의 브랜드 집중도는 어때", revised, {"fact_md": fact_md})["status"] == "pass"


def test_concentration_backfills_market_scope_when_planner_only_fetched_metric() -> None:
    calls = [{"tool": "get_brand_metric", "render_data": {"brand": "리바로", "sales_억원": 84.93}}]
    plans = answer_contract_backfill_tool_calls("리바로 시장의 브랜드 집중도는 어때", "리바로", calls)
    assert len(plans) == 1
    assert plans[0].name == "get_market_scope"
    assert plans[0].arguments == {"brand": "리바로", "view": "market_landscape"}


def test_target_share_gap_adds_full_deterministic_calculation() -> None:
    revised = enforce_answer_contract("리바로 점유율 4% 달성에 필요한 매출", "현재 점유율은 3.76%입니다.", {"fact_md": TARGET_FACT})
    assert "시장 규모 2,256.77억원" in revised
    assert "목표 매출 90.27억원" in revised
    assert "증분액 +5.34억원" in revised
    assert "증분률 +6.29%" in revised
    assert "시장 규모 불변 가정" in revised


def test_target_share_gap_combines_live_split_fact_tables() -> None:
    revised = enforce_answer_contract("리바로 점유율 4% 달성에 필요한 매출", "현재 점유율은 3.76%입니다.", {"fact_md": TARGET_SPLIT_FACT})
    assert "목표 매출 90.27억원" in revised
    assert evaluate_answer_contract(
        "리바로 점유율 4% 달성에 필요한 매출", revised, {"fact_md": TARGET_SPLIT_FACT}
    )["status"] == "pass"


def test_channel_provenance_echoes_only_verified_filter() -> None:
    revised = enforce_answer_contract("의원 채널에서 리바로 매출", "리바로 매출 결과입니다.", {"fact_md": CHANNEL_FACT})
    assert "적용 채널: 의원" in revised


@pytest.mark.parametrize(
    ("question", "fact_md"),
    (
        ("리바로와 리바로젯 6개월 매출 비교", PAIR_FACT),
        ("상위 3개 브랜드 점유율 변화를 비교해줘", TOP_FACT),
        ("상위 5개 브랜드 합산 점유율", TOP_FACT),
        ("이 시장의 브랜드 집중도는 어때", TOP_FACT),
        ("리바로 점유율 4% 달성에 필요한 매출", TARGET_FACT),
        ("의원 채널에서 리바로 매출", CHANNEL_FACT),
    ),
)
def test_complete_repairs_are_byte_idempotent(question: str, fact_md: str) -> None:
    repaired = enforce_answer_contract(question, "미완성 답변", {"fact_md": fact_md})
    assert enforce_answer_contract(question, repaired, {"fact_md": fact_md}) == repaired


@pytest.mark.parametrize(
    "question",
    (
        "리바로 최근 매출 알려줘",
        "리바로 시장 순위 알려줘",
        "리바로 분기별 추이 알려줘",
        "리바로 시장에서 상위 브랜드 뭐 있어",
    ),
)
def test_unrelated_complete_answers_are_byte_unchanged(question: str) -> None:
    answer = "이미 정상인 답변입니다.\n\n## 출처\n- 데이터: UBIST"
    assert enforce_answer_contract(question, answer, {"fact_md": TOP_FACT}) == answer


@pytest.mark.parametrize(
    "question",
    (
        "리바로와 리바로젯 매출 비교",
        "상위 5개 브랜드 합산 점유율",
        "리바로 점유율 4% 달성에 필요한 매출",
    ),
)
def test_missing_fact_never_fabricates_completion(question: str) -> None:
    answer = "확인 가능한 데이터가 없습니다."
    assert enforce_answer_contract(question, answer, None) == answer
