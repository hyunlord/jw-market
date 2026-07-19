from __future__ import annotations

import pytest

from jw_chat_agent_poc.tools.metrics.cache_live import StaticCausePayloadReader, StaticMetricsCacheReader
from jw_chat_agent_poc.tools.metrics.cd_mart import CdBrandLink, StaticCdMartReader
from jw_chat_agent_poc.tools.metrics.market_scope import (
    MarketScopeResolver,
    detect_market_scope_intent,
    map_market_view_reply,
)
from jw_chat_agent_poc.tools.query_layer import MartRecord, StaticStrategicMartReader, StrategicQueryLayer

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


class RecordingGeneralViewService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool, bool]] = []

    def answer(self, question: str, *, compact: bool, dual: bool) -> dict:
        self.calls.append((question, compact, dual))
        return {
            "question": question,
            "tool_calls": [{"tool": "general_view_dynamic_market", "source": "UBIST"}],
            "answer": "## 일반뷰 (ATC4)\n\n동적 일반뷰 결과",
            "sources": ["UBIST"],
        }


def test_detect_market_scope_intent_defaults_to_market_landscape() -> None:
    intent = detect_market_scope_intent("리바로랑 같은 시장 매출")

    assert intent is not None
    assert intent.brand_hint == "리바로"
    assert intent.view_type == "market_landscape"
    assert intent.requires_clarification is False


@pytest.mark.parametrize(
    "question",
    (
        "리바로가 속한 시장 매출",
        "리바로 시장 규모",
        "리바로가 소속된 시장 전체 매출",
        "리바로가 포함된 시장 총매출",
        "리바로 시장의 전체 규모",
    ),
)
def test_detect_market_scope_intent_handles_semantic_paraphrases(question: str) -> None:
    intent = detect_market_scope_intent(question)

    assert intent is not None
    assert intent.brand_hint == "리바로"
    assert intent.view_type == "market_landscape"


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
    assert "전략뷰 (market_landscape)" in result["answer"]
    assert "## 주의" not in result["answer"]
    assert "84.93억원" not in result["answer"]
    assert call["qa_trace"]["status"] == "ok"
    assert call["qa_trace"]["started_at"]
    assert call["qa_trace"]["ended_at"]
    assert call["qa_trace"]["row_count"] > 0
    assert call["qa_trace"]["data_as_of"] == "2026-04"
    assert call["qa_trace"]["cache_hit"] is True


def test_market_scope_uses_query_layer_without_legacy_cause_reader() -> None:
    def record(brand: str, value: float) -> MartRecord:
        return MartRecord(
            ml_id="ml_006",
            brand_name=brand,
            source="ubist",
            measure="sales",
            metric_history={"2026-05": {"raw_value": value}},
            channel_data={},
            specialty_data={},
            dimension_data={},
            by_dimension={},
        )

    records = (
        record("리바로", 8_038_598_800.0),
        record("로수젯", 19_523_856_200.0),
    )
    resolver = MarketScopeResolver(
        cache_reader=StaticMetricsCacheReader(cache_brands=CACHE_BRANDS, market_status={}),
        query_layer=StrategicQueryLayer(reader=StaticStrategicMartReader(records)),
    )

    result = resolver.answer("리바로랑 같은 시장 매출", view_type="market_landscape")

    call = result["tool_calls"][0]
    assert call["source"] == "UBIST"
    assert call["render_data"]["period"] == "2026-05"
    assert call["render_data"]["brand_sales_krw"] == 8_038_598_800.0
    assert call["render_data"]["market_size_recent_krw"] == 27_562_455_000.0
    assert result["sources"] == ["UBIST"]
    assert call["qa_trace"]["status"] == "ok"
    assert call["qa_trace"]["started_at"]
    assert call["qa_trace"]["ended_at"]
    assert call["qa_trace"]["row_count"] > 0
    assert call["qa_trace"]["data_as_of"] == "2026-05"
    assert call["qa_trace"]["cache_hit"] is False


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


def test_market_scope_general_view_delegates_to_dynamic_mart() -> None:
    general_view = RecordingGeneralViewService()
    resolver = MarketScopeResolver(
        cache_reader=StaticMetricsCacheReader(cache_brands=CACHE_BRANDS, market_status=BRAND_CARDS),
        general_view_service=general_view,
    )

    result = resolver.answer("리바로 같은 시장 일반뷰", view_type="general_view")

    assert general_view.calls == [("리바로 같은 시장 일반뷰", False, False)]
    assert result["tool_calls"][0]["tool"] == "general_view_dynamic_market"
    assert "동적 일반뷰 결과" in result["answer"]
