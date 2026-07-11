from __future__ import annotations

from jw_chat_agent_poc.tools.metrics.cache_live import StaticCausePayloadReader, StaticMetricsCacheReader
from jw_chat_agent_poc.tools.metrics.cd_mart import CdBrandLink, StaticCdMartReader
from jw_chat_agent_poc.tools.metrics.market_scope import (
    MarketScopeResolver,
    detect_market_scope_intent,
    map_market_view_reply,
)

from test_metrics_cache import BRAND_CARDS, CACHE_BRANDS, CAUSE_PAYLOAD


CD_MART_SERIES = {
    "2025-04": 32_342_749_925.11,
    "2026-04": 34_833_057_844.92,
}


def _cd_mart_reader() -> StaticCdMartReader:
    return StaticCdMartReader(
        brand_links=(CdBrandLink("리바로", "cd_006", "ubist", "ml_006"),),
        market_series={("cd_006", "ubist"): CD_MART_SERIES},
    )


def _resolver() -> MarketScopeResolver:
    cache_reader = StaticMetricsCacheReader(cache_brands=CACHE_BRANDS, market_status=BRAND_CARDS)
    cause_reader = StaticCausePayloadReader(
        {
            ("리바로", "market_landscape", "UBIST", "sales", "strategy_006"): CAUSE_PAYLOAD,
        }
    )
    return MarketScopeResolver(cache_reader=cache_reader, cause_reader=cause_reader, cd_mart_reader=_cd_mart_reader())


def test_detect_market_scope_intent_defaults_to_market_landscape() -> None:
    intent = detect_market_scope_intent("리바로랑 같은 시장 매출")

    assert intent is not None
    assert intent.brand_hint == "리바로"
    assert intent.view_type == "market_landscape"
    assert intent.requires_clarification is False


def test_detect_market_scope_intent_answers_strong_view_question_with_default_view() -> None:
    intent = detect_market_scope_intent("리바로랑 같은 시장 매출은 어느 기준으로 봐야 해?")

    assert intent is not None
    assert intent.requires_clarification is False
    assert intent.view_type == "market_landscape"


def test_map_market_view_reply_is_deterministic_and_bounded() -> None:
    assert map_market_view_reply("전략뷰") == "market_landscape"
    assert map_market_view_reply("경쟁군 기준으로") == "competitive_dynamics"
    assert map_market_view_reply("일반뷰") == "general_view"
    assert map_market_view_reply("그냥 리바로 매출") is None


def test_market_scope_default_answer_uses_market_total_not_brand_sales() -> None:
    result = _resolver().answer("리바로랑 같은 시장 매출", view_type="market_landscape")

    call = result["tool_calls"][0]
    data = call["render_data"]
    assert result["sources"] == ["cache"]
    assert call["tool"] == "get_market_landscape"
    assert data["scope"] == "market"
    assert data["view_type"] == "market_landscape"
    assert data["market_size_recent_krw"] == 225_677_368_890.97986
    assert data["brand_sales_krw"] == 8_493_234_217.11
    assert "전략뷰 기준" in result["answer"]
    assert "competitive_dynamics" not in result["answer"]
    assert "market_landscape" not in result["answer"]
    assert "## 주의" not in result["answer"]
    assert "84.93억원" not in result["answer"]


def test_market_scope_clarification_does_not_show_internal_view_enums() -> None:
    result = _resolver().clarification("리바로랑 같은 시장 매출은 어느 기준?", brand="리바로")

    assert "전략뷰" in result["answer"]
    assert "경쟁군" in result["answer"]
    assert "market_landscape" not in result["answer"]
    assert "competitive_dynamics" not in result["answer"]


def test_market_scope_competitive_dynamics_uses_cd_mart_series() -> None:
    result = _resolver().answer("리바로 같은 시장 경쟁군 기준", view_type="competitive_dynamics")

    data = result["tool_calls"][0]["render_data"]
    assert data["view_type"] == "competitive_dynamics"
    assert data["market_size_recent_krw"] == 34_833_057_844.92
    assert data["period"] == "2026-04"
    assert "경쟁군 기준" in result["answer"]


def test_market_scope_general_view_is_transparent_unsupported() -> None:
    result = _resolver().unsupported_general_view("리바로 같은 시장 일반뷰")

    call = result["tool_calls"][0]
    assert call["tool"] == "unsupported_metric"
    assert call["render_data"]["status"] == "unsupported"
    assert call["render_data"]["unsupported_filters"][0]["field"] == "view_type"
    assert "일반뷰(atc4) 기준 시장 데이터는 현재 채팅 데이터에 없습니다" in result["answer"]
