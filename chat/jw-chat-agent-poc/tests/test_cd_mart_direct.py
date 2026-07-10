from __future__ import annotations

from typing import Any

import pytest

import jw_chat_agent_poc.service  # noqa: F401  # pre-import: market_scope↔service.app 순환 import 회피(기존 suite와 동일한 로드 순서)
from jw_chat_agent_poc.tools.metrics.cache_live import StaticCausePayloadReader, StaticMetricsCacheReader
from jw_chat_agent_poc.tools.metrics.cd_mart import (
    CdBrandLink,
    StaticCdMartReader,
    mart_source_key,
    series_with_yoy,
    strategy_id_from_ml,
)
from jw_chat_agent_poc.tools.metrics.market_scope import MarketScopeResolver

from test_metrics_cache import BRAND_CARDS, CACHE_BRANDS, CAUSE_PAYLOAD


CD_RAW_SERIES = {
    "2025-04": 30_000_000_000.0,
    "2025-05": 30_500_000_000.0,
    "2025-06": 31_000_000_000.0,
    "2025-07": 31_200_000_000.0,
    "2025-08": 31_400_000_000.0,
    "2025-09": 31_600_000_000.0,
    "2025-10": 31_900_000_000.0,
    "2025-11": 32_100_000_000.0,
    "2025-12": 32_400_000_000.0,
    "2026-01": 32_800_000_000.0,
    "2026-02": 33_100_000_000.0,
    "2026-03": 33_600_000_000.0,
    "2026-04": 34_833_057_844.92,
}


def _builder_reference_series(series: dict[str, Any]) -> dict[str, dict[str, float | None]]:
    """Reference re-implementation of the cache_cause builder's market_size_series_with_yoy."""

    periods = sorted(series.keys())
    step = 12 if any("-Q" not in str(period) for period in periods) else 4
    yoy: dict[str, float | None] = {}
    for index, period in enumerate(periods):
        current = series.get(period)
        previous = series.get(periods[index - step]) if index >= step else None
        if current is None or previous in (None, 0):
            yoy[str(period)] = None
        else:
            yoy[str(period)] = round((current - previous) / previous * 100, 4)
    return {
        str(period): {"value": float(series[period]), "yoy_growth_pct": yoy[str(period)]}
        for period in periods
    }


def _cd_mart_reader(
    links: tuple[CdBrandLink, ...] | None = None,
    series: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> StaticCdMartReader:
    return StaticCdMartReader(
        brand_links=links or (CdBrandLink("리바로", "cd_006", "ubist", "ml_006"),),
        market_series=series or {("cd_006", "ubist"): CD_RAW_SERIES},
    )


def _resolver(reader: StaticCdMartReader | None = None) -> MarketScopeResolver:
    cache_reader = StaticMetricsCacheReader(cache_brands=CACHE_BRANDS, market_status=BRAND_CARDS)
    cause_reader = StaticCausePayloadReader(
        {
            ("리바로", "market_landscape", "UBIST", "sales", "strategy_006"): CAUSE_PAYLOAD,
        }
    )
    return MarketScopeResolver(
        cache_reader=cache_reader,
        cause_reader=cause_reader,
        cd_mart_reader=reader or _cd_mart_reader(),
    )


def test_series_with_yoy_matches_cache_cause_builder_semantics() -> None:
    assert series_with_yoy(CD_RAW_SERIES) == _builder_reference_series(CD_RAW_SERIES)


def test_series_with_yoy_uses_quarter_step_for_quarterly_series() -> None:
    quarterly = {
        "2025-Q1": 100.0,
        "2025-Q2": 110.0,
        "2025-Q3": 120.0,
        "2025-Q4": 130.0,
        "2026-Q1": 121.0,
    }
    result = series_with_yoy(quarterly)
    assert result["2026-Q1"]["yoy_growth_pct"] == 21.0
    assert result["2025-Q4"]["yoy_growth_pct"] is None


def test_cd_answer_uses_mart_series_value_and_yoy() -> None:
    result = _resolver().answer("리바로 같은 시장 경쟁군 기준", view_type="competitive_dynamics")

    data = result["tool_calls"][0]["render_data"]
    expected = _builder_reference_series(CD_RAW_SERIES)["2026-04"]
    assert data["view_type"] == "competitive_dynamics"
    assert data["period"] == "2026-04"
    assert data["market_size_recent_krw"] == expected["value"]
    assert data["yoy_growth_pct"] == expected["yoy_growth_pct"]


def test_cd_filter_semantics_preserved_cd_series_is_narrower_than_ml() -> None:
    cd_result = _resolver().answer("리바로 같은 시장 경쟁군 기준", view_type="competitive_dynamics")
    ml_result = _resolver().answer("리바로 같은 시장 전략뷰 기준", view_type="market_landscape")

    cd_size = cd_result["tool_calls"][0]["render_data"]["market_size_recent_krw"]
    ml_size = ml_result["tool_calls"][0]["render_data"]["market_size_recent_krw"]
    assert cd_size == 34_833_057_844.92
    assert ml_size != cd_size
    assert cd_size < ml_size


def test_cd_brand_in_multiple_markets_resolves_by_requested_market() -> None:
    links = (
        CdBrandLink("리바로", "cd_006", "ubist", "ml_006"),
        CdBrandLink("리바로", "cd_099", "ubist", "ml_099"),
    )
    series = {
        ("cd_006", "ubist"): CD_RAW_SERIES,
        ("cd_099", "ubist"): {"2026-04": 1.0},
    }
    result = _resolver(_cd_mart_reader(links, series)).answer("리바로 경쟁군", view_type="competitive_dynamics")

    assert result["tool_calls"][0]["render_data"]["market_size_recent_krw"] == 34_833_057_844.92


def test_cd_market_mismatch_is_unsupported_not_wrong_market() -> None:
    links = (CdBrandLink("리바로", "cd_777", "ubist", "ml_777"),)
    series = {("cd_777", "ubist"): CD_RAW_SERIES}
    result = _resolver(_cd_mart_reader(links, series)).answer("리바로 경쟁군", view_type="competitive_dynamics")

    call = result["tool_calls"][0]
    assert call["tool"] == "unsupported_metric"
    assert "cache_cause" not in result["answer"]


def test_cd_missing_brand_is_unsupported_without_cache_cause_wording() -> None:
    reader = _cd_mart_reader(links=(CdBrandLink("다른브랜드", "cd_001", "ubist", "ml_001"),))
    result = _resolver(reader).answer("리바로 경쟁군", view_type="competitive_dynamics")

    call = result["tool_calls"][0]
    assert call["tool"] == "unsupported_metric"
    assert "cache_cause" not in result["answer"]


def test_mart_source_key_maps_api_labels() -> None:
    assert mart_source_key("UBIST") == "ubist"
    assert mart_source_key("IQVIA") == "iqvia_nsa"
    assert mart_source_key("iqvia_nsa") == "iqvia_nsa"
    assert mart_source_key("") == "ubist"


def test_strategy_id_from_ml_matches_builder_rule() -> None:
    assert strategy_id_from_ml("ml_006") == "strategy_006"
    assert strategy_id_from_ml("ml_19") == "strategy_019"
    assert strategy_id_from_ml("") == ""


def test_market_landscape_still_uses_cache_cause_payload() -> None:
    result = _resolver().answer("리바로 같은 시장 전략뷰", view_type="market_landscape")

    data = result["tool_calls"][0]["render_data"]
    assert data["view_type"] == "market_landscape"
    expected = CAUSE_PAYLOAD["data"]["sources_data"]["market_size_series"]
    latest_period = sorted(expected)[-1]
    assert data["market_size_recent_krw"] == expected[latest_period]["value"]


def test_snapshot_lookup_error_for_missing_series() -> None:
    reader = _cd_mart_reader(series={("cd_006", "ubist"): {}})
    snapshot = reader.load()
    with pytest.raises(LookupError):
        snapshot.market_size_series(brand="리바로", source="UBIST", market_id="strategy_006")
