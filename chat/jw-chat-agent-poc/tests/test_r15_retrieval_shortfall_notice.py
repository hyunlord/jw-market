"""R15 STAGE 1 — the surface must separate "no data" from "we never got it back"."""

from __future__ import annotations

from jw_chat_agent_poc.service.v4.contracts import SourceResult
from jw_chat_agent_poc.service.v4.runtime import _retrieval_shortfall_notice


def _mart(query: str, status: str, notice: str | None = None) -> SourceResult:
    return SourceResult(source="mart", query=query, status=status, notice=notice)


LIVE_MART_SHAPE = (
    _mart("리바로 매출 알려줘", "ok"),
    _mart("리바로제트 매출 알려줘", "empty", "mart read-only adapters returned no rows"),
    _mart("피타바스타틴 매출 알려줘", "timeout", "응답 지연으로 미포함"),
    _mart("JW중외제약 매출 알려줘", "empty", "mart read-only adapters returned no rows"),
)


def test_all_ok_lane_stays_silent() -> None:
    notice = _retrieval_shortfall_notice(
        (_mart("리바로 매출 알려줘", "ok"), _mart("로수젯 매출 알려줘", "ok"))
    )
    assert notice is None


def test_timeout_and_empty_are_reported_separately() -> None:
    notice = _retrieval_shortfall_notice(LIVE_MART_SHAPE)
    assert notice is not None
    assert "시장 데이터 조회 4건 중 1건에서 자료를 확보했습니다." in notice
    timeout_line = next(
        line for line in notice.splitlines() if "조회 시간이 초과" in line
    )
    empty_line = next(
        line for line in notice.splitlines() if "해당하는 자료를 찾지 못했습니다" in line
    )
    assert timeout_line != empty_line
    assert "1건은" in timeout_line
    assert "피타바스타틴 매출 알려줘" in timeout_line
    assert "2건은" in empty_line
    assert "리바로제트 매출 알려줘" in empty_line
    assert "JW중외제약 매출 알려줘" in empty_line


def test_internal_reason_codes_never_reach_the_surface() -> None:
    notice = _retrieval_shortfall_notice(LIVE_MART_SHAPE) or ""
    for code in (
        "per_tool_timeout",
        "total_timeout",
        "upstream_timeout",
        "empty_result",
        "provider_quota",
        "parse_error",
        "scope_limit",
        "upstream_error",
        "soft_deadline",
    ):
        assert code not in notice


def test_every_call_timing_out_is_not_reported_as_absence() -> None:
    results = tuple(
        _mart(f"리바로 {index}년 매출", "timeout", "응답 지연으로 미포함")
        for index in range(12)
    )
    notice = _retrieval_shortfall_notice(results)
    assert notice is not None
    assert "시장 데이터 조회 12건 중 0건에서 자료를 확보했습니다." in notice
    assert "12건은 조회 시간이 초과되어 이번 답변에 반영되지 않았습니다" in notice


def test_query_preview_is_bounded_but_the_count_is_not() -> None:
    results = tuple(
        _mart(f"질의{index}", "empty", "mart read-only adapters returned no rows")
        for index in range(9)
    )
    notice = _retrieval_shortfall_notice(results) or ""
    assert "9건은" in notice
    assert "외 4건" in notice


def test_unknown_reason_is_still_counted_rather_than_dropped() -> None:
    # ``deadline_exceeded`` maps onto a typed reason today; an untyped status
    # must still surface as a shortfall instead of silently vanishing.
    results = (
        _mart("리바로 매출 알려줘", "ok"),
        _mart("로수젯 매출 알려줘", "error"),
    )
    notice = _retrieval_shortfall_notice(results) or ""
    assert "시장 데이터 조회 2건 중 1건에서 자료를 확보했습니다." in notice
    assert "1건은" in notice
    assert "로수젯 매출 알려줘" in notice


def test_lanes_are_reported_independently() -> None:
    results = (
        _mart("리바로 매출 알려줘", "ok"),
        _mart("피타바스타틴 매출 알려줘", "timeout", "응답 지연으로 미포함"),
        SourceResult(source="hira", query="리바로 처방 실적", status="empty"),
    )
    notice = _retrieval_shortfall_notice(results) or ""
    assert "시장 데이터 조회 2건 중 1건에서 자료를 확보했습니다." in notice
    assert "HIRA 조회 1건 중 0건에서 자료를 확보했습니다." in notice


def test_f1_failure_injection_without_the_notice_the_shortfall_disappears() -> None:
    """F1 (negative arm): the pre-R15 surface said nothing about executed calls."""
    surviving_notice_inputs = tuple(
        result for result in LIVE_MART_SHAPE if result.status == "ok"
    )
    assert _retrieval_shortfall_notice(surviving_notice_inputs) is None
    assert _retrieval_shortfall_notice(LIVE_MART_SHAPE) is not None
