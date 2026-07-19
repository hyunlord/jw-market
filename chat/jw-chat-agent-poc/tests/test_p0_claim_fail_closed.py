from __future__ import annotations

import pytest

from jw_chat_agent_poc.orchestrator.market_answer_contract import enforce_market_answer_contract
from jw_chat_agent_poc.service.answer_safety import (
    enforce_relational_numeric_claims,
    enforce_relational_numeric_claims_with_trace,
)


def _series_call(
    *,
    brand: str = "마운자로",
    status: str = "ok",
    periods: tuple[str, ...] = ("2025-Q1", "2025-Q2", "2025-Q3", "2025-Q4"),
    values: tuple[float, ...] = (100.0, 110.0, 120.0, 130.0),
    metric: str = "sales",
    cache_hit: bool = False,
    data_as_of: str = "2025-Q4",
) -> dict[str, object]:
    rows = [
        {
            "period": period,
            "value_억원": value,
            **({"ms_pct": value / 10} if metric == "market_share" else {}),
        }
        for period, value in zip(periods, values, strict=True)
    ]
    return {
        "tool": "get_brand_share" if metric == "market_share" else "get_brand_series",
        "status": status,
        "cache_hit": cache_hit,
        "data_as_of": data_as_of,
        "render_data": {
            "status": status,
            "brand": brand,
            "metric": metric,
            "period": data_as_of,
            "cache_hit": cache_hit,
            "data_as_of": data_as_of,
            "brand_value_series_10pt": rows,
        },
    }


def _failed_call(*, brand: str = "마운자로", status: str = "query_failed", metric: str = "sales") -> dict[str, object]:
    return {
        "tool": "get_brand_share" if metric == "market_share" else "get_brand_series",
        "status": status,
        "render_data": {
            "status": status,
            "brand": brand,
            "metric": metric,
            "error": "injected failure",
        },
    }


@pytest.mark.parametrize("status", ("query_failed", "error", "timeout", "no_data"))
def test_all_failed_metric_blocks_relation_and_returns_nonempty_typed_fallback(status: str) -> None:
    revised = enforce_relational_numeric_claims(
        "마운자로 매출 추이",
        "마운자로 매출은 최근 3분기 연속 상승했습니다.",
        [_failed_call(status=status)],
    )

    assert revised.strip()
    assert "상태: 확인 불가" in revised
    assert "사유:" in revised
    assert "확인된 범위: 없음" in revised
    assert "대안:" in revised
    assert "연속 상승" not in revised


@pytest.mark.parametrize(
    ("question", "unsupported_claim", "forbidden_fragment"),
    (
        ("마운자로 매출 추이", "마운자로 매출은 반등했습니다.", "반등"),
        ("마운자로 매출 추이", "마운자로 매출은 2025-Q4가 정점입니다.", "정점"),
        ("마운자로 순위", "마운자로가 크레스토를 추월했습니다.", "추월"),
        ("마운자로 시장 위치", "마운자로가 시장에서 우위입니다.", "우위"),
        ("마운자로 순위", "마운자로 순위는 7위에서 3위로 변했습니다.", "7위에서 3위"),
        ("마운자로 시장 대비 성장", "마운자로가 시장보다 빠르게 성장했습니다.", "빠르게 성장"),
    ),
)
def test_failed_metric_blocks_all_relational_claim_families(
    question: str,
    unsupported_claim: str,
    forbidden_fragment: str,
) -> None:
    gate = enforce_relational_numeric_claims_with_trace(
        question,
        unsupported_claim,
        [_failed_call()],
    )

    assert gate.answer.strip()
    assert "상태: 확인 불가" in gate.answer
    assert forbidden_fragment not in gate.answer
    assert gate.blocked_claim_count >= 1
    assert gate.blocked_reasons


def test_partial_tool_failure_keeps_sales_claim_and_blocks_failed_share_claim() -> None:
    sales = _series_call()
    failed_share = _failed_call(metric="market_share")
    answer = (
        "마운자로 매출은 최근 3분기 연속 상승했습니다.\n\n"
        "마운자로 점유율은 최근 3분기 연속 상승했습니다."
    )

    revised = enforce_relational_numeric_claims(
        "마운자로 매출과 점유율 추이",
        answer,
        [sales, failed_share],
    )

    assert "마운자로 매출은 최근 3분기 연속 상승했습니다" in revised
    assert "마운자로 점유율은 최근 3분기 연속 상승했습니다" not in revised
    assert "점유율" in revised and "확인 불가" in revised


def test_market_contract_does_not_promote_one_failed_tool_to_whole_answer_failure() -> None:
    revised = enforce_market_answer_contract(
        question="마운자로 매출과 점유율 추이",
        answer="마운자로 매출은 2025-Q4 130.00억원입니다.",
        tool_calls=[_series_call(), _failed_call(metric="market_share")],
    )

    assert "2025-Q4" in revised
    assert "130" in revised
    assert not revised.startswith("데이터 존재 여부를 확인하지 못했습니다. 조회 오류입니다.")


def test_current_value_without_history_cannot_support_a_trend_claim() -> None:
    current = _series_call(periods=("2026-05",), values=(130.0,), data_as_of="2026-05")

    revised = enforce_relational_numeric_claims(
        "마운자로 매출 추이",
        "2026-05 마운자로 매출은 130억원이며 최근 3개월 연속 상승했습니다.",
        [current],
    )

    assert "2026-05" in revised
    assert "130억원" in revised
    assert "연속 상승" not in revised


def test_nonconsecutive_periods_cannot_support_a_consecutive_streak() -> None:
    gapped = _series_call(
        periods=("2025-Q1", "2025-Q3", "2025-Q4"),
        values=(100.0, 120.0, 130.0),
    )

    revised = enforce_relational_numeric_claims(
        "마운자로 매출 추이",
        "마운자로 매출은 최근 2분기 연속 상승했습니다.",
        [gapped],
    )

    assert "최근 2분기 연속 상승" not in revised
    assert "직전 분기 대비 상승" in revised


def test_failed_live_call_with_cached_fallback_discloses_as_of_and_avoids_recency_claim() -> None:
    cached = _series_call(cache_hit=True, data_as_of="2025-Q4")

    revised = enforce_relational_numeric_claims(
        "마운자로 최근 매출 추이",
        "마운자로 매출은 최근 3분기 연속 상승했습니다.",
        [_failed_call(status="timeout"), cached],
    )

    assert "실시간 조회 실패" in revised
    assert "2025-Q4 기준 저장 결과" in revised
    assert "최근" not in revised
    assert "현재" not in revised
    assert "최신" not in revised


def test_clarification_is_not_treated_as_tool_failure() -> None:
    answer = "어느 브랜드의 매출 추이인지 알려주세요."
    clarify = {
        "tool": "clarify",
        "status": "needs_clarification",
        "render_data": {"status": "needs_clarification"},
    }

    assert enforce_relational_numeric_claims("매출 추이", answer, [clarify]) == answer


def test_supported_normal_trend_remains_unchanged() -> None:
    answer = "마운자로 매출은 최근 3분기 연속 상승했습니다."

    assert enforce_relational_numeric_claims(
        "마운자로 매출 추이",
        answer,
        [_series_call()],
    ) == answer
