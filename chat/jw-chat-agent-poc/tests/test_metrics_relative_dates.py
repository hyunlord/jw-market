from __future__ import annotations

from pytest import MonkeyPatch, approx

from jw_chat_agent_poc import ChatAgent
from jw_chat_agent_poc.tools.metrics import relative_periods
from jw_chat_agent_poc.tools.metrics import MetricsTool
from jw_chat_agent_poc.tools.metrics.cache_live import StaticCausePayloadReader, StaticMetricsCacheReader


BRAND_CARDS = {
    "brand_cards": [
        {
            "brand": "리바로",
            "market_id": "strategy_006",
            "market_name": "리바로/리바로젯 시장",
            "front": {
                "value_recent": 8_493_234_217.11,
                "ms_recent_pct": 3.7634,
                "default_source": "UBIST",
            },
        }
    ]
}

CACHE_BRANDS = [{"brand": "리바로", "market_id": "strategy_006", "sources": ["UBIST"], "rank": 1}]

PERIODS = [
    "2025-01",
    "2025-11",
    "2025-12",
    "2026-01",
    "2026-02",
    "2026-03",
    "2026-04",
]

MARKET_VALUES = [
    100_000_000_000,
    140_000_000_000,
    150_000_000_000,
    197_000_000_000,
    198_000_000_000,
    228_838_670_570,
    225_677_368_890.97986,
]

BRAND_VALUES = [
    7_000_000_000,
    8_000_000_000,
    9_000_000_000,
    7_500_000_000,
    7_700_000_000,
    8_711_248_139.54,
    8_493_234_217.11,
]

CAUSE_PAYLOAD = {
    "data": {
        "meta": {"brand": "리바로", "market_id": "strategy_006"},
        "sources_data": {
            "market_size_series": {
                period: {"value": value}
                for period, value in zip(PERIODS, MARKET_VALUES, strict=True)
            },
        },
        "level_top5_trend": {
            "by_level": {
                "Brand": {
                    "periods_10pt": PERIODS,
                    "values": [
                        {
                            "brands_in_value": [
                                {
                                    "brand": "리바로",
                                    "value_series_10pt": BRAND_VALUES,
                                    "ms_series_10pt": [
                                        7.0,
                                        5.7143,
                                        6.0,
                                        3.8071,
                                        3.8889,
                                        3.8067,
                                        3.7634,
                                    ],
                                    "rank_series_10pt": [5, 5, 5, 6, 6, 6, 6],
                                }
                            ]
                        }
                    ],
                }
            }
        },
    },
}

CAUSE_READER = StaticCausePayloadReader(
    {
        ("리바로", "market_landscape", "UBIST", "sales", "strategy_006"): CAUSE_PAYLOAD,
    }
)


def _agent() -> ChatAgent:
    reader = StaticMetricsCacheReader(cache_brands=CACHE_BRANDS, market_status=BRAND_CARDS)
    metrics = MetricsTool(
        mode="cache",
        cache_reader=reader,
        cause_reader=CAUSE_READER,
    )
    return ChatAgent(metrics=metrics)


def test_chat_agent_resolves_months_ago_from_current_month(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(relative_periods, "_current_month", lambda: "2026-06")

    result = _agent().answer("리바로 3달전 매출")
    call = result["tool_calls"][0]

    assert call["tool"] == "get_brand_metric"
    data = call["render_data"]
    assert data["period"] == "2026-03"
    assert data["sales_krw"] == 8_711_248_139.54
    assert data["market_size_filtered_krw"] == 228_838_670_570.0
    assert data["applied_filters"]["period_month"] == "2026-03"
    assert data["interpretation_notes"] == [
        {
            "requested": "3달전",
            "interpreted_as": "2026-03",
            "basis": "현재 2026-06 기준 계산",
        }
    ]
    assert "3달전 → 2026-03" in result["answer"]
    assert "현재 2026-06 기준 계산" in result["answer"]


def test_chat_agent_resolves_months_ago_current_month_with_available_data(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(relative_periods, "_current_month", lambda: "2026-06")

    result = _agent().answer("리바로 6달전 매출")
    call = result["tool_calls"][0]

    assert call["tool"] == "get_brand_metric"
    data = call["render_data"]
    assert data["period"] == "2025-12"
    assert data["sales_krw"] == 9_000_000_000.0
    assert data["market_size_filtered_krw"] == 150_000_000_000.0
    assert data["interpretation_notes"] == [
        {
            "requested": "6달전",
            "interpreted_as": "2025-12",
            "basis": "현재 2026-06 기준 계산",
        }
    ]


def test_chat_agent_resolves_recent_month_range_from_current_month_with_latest_cutoff(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(relative_periods, "_current_month", lambda: "2026-06")

    result = _agent().answer("리바로 최근 6개월 매출")
    call = result["tool_calls"][0]

    assert call["tool"] == "get_brand_metric"
    data = call["render_data"]
    assert data["period"] == "2025-12~2026-04"
    assert data["sales_krw"] == 41_404_482_356.65
    assert data["market_size_filtered_krw"] == approx(999_516_039_460.98)
    assert data["applied_filters"]["period_range"] == "2025-12~2026-04"
    assert data["interpretation_notes"] == [
        {
            "requested": "최근 6개월",
            "interpreted_as": "2025-12~2026-04",
            "basis": "현재 2026-06 기준 요청구간 2025-12~2026-05 중 최신 2026-04까지 제공",
        }
    ]


def test_chat_agent_reports_latest_after_current_relative_month_as_unavailable(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(relative_periods, "_current_month", lambda: "2026-06")

    result = _agent().answer("리바로 1달전 매출")
    call = result["tool_calls"][0]

    assert call["tool"] == "unsupported_metric"
    data = call["render_data"]
    assert data["status"] == "unsupported"
    assert data["unsupported"][0]["field"] == "relative_period"
    assert data["unsupported"][0]["value"] == "1달전"
    assert "2026-05 데이터는 아직 없습니다" in data["unsupported"][0]["reason"]
    assert "최신은 2026-04까지" in data["unsupported"][0]["reason"]
    assert "84.93억원" not in result["answer"]


def test_chat_agent_reports_daily_relative_date_as_unsupported_without_latest_fallback(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(relative_periods, "_current_month", lambda: "2026-06")

    result = _agent().answer("리바로 오늘 매출")
    call = result["tool_calls"][0]

    assert call["tool"] == "unsupported_metric"
    data = call["render_data"]
    assert data["status"] == "unsupported"
    assert data["unsupported"][0]["field"] == "relative_period"
    assert data["unsupported"][0]["value"] == "오늘"
    assert "월 단위" in data["unsupported"][0]["reason"]
    assert "2026-06 데이터는 아직 없습니다" in data["unsupported"][0]["reason"]
    assert "최신은 2026-04까지" in data["unsupported"][0]["reason"]
    assert data["data_basis"]["first_period"] == "2025-01"
    assert data["data_basis"]["latest_period"] == "2026-04"
    assert "84.93억원" not in result["answer"]
    assert "지원 안 됨: 상대 날짜" in result["answer"]
    assert "relative_period" not in result["answer"]


def test_chat_agent_reports_out_of_range_relative_date_with_available_range(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(relative_periods, "_current_month", lambda: "2026-06")

    result = _agent().answer("리바로 5년전 매출")
    call = result["tool_calls"][0]

    assert call["tool"] == "unsupported_metric"
    data = call["render_data"]
    assert data["unsupported"][0]["field"] == "relative_period"
    assert data["unsupported"][0]["value"] == "5년전"
    assert "2025-01~2026-04" in data["unsupported"][0]["reason"]
    assert "84.93억원" not in result["answer"]
