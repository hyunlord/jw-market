from __future__ import annotations

import pytest

from jw_chat_agent_poc.tools.metrics.cache_live import StaticMetricsCacheReader
from jw_chat_agent_poc.tools.metrics.cd_mart import CdBrandLink, StaticCdMartReader
from jw_chat_agent_poc.tools.metrics.market_scope import (
    MarketScopeResolver,
    detect_market_scope_intent,
    map_market_view_reply,
)
from jw_chat_agent_poc.tools.query_layer import MartRecord, StaticStrategicMartReader, StrategicQueryLayer
from jw_chat_agent_poc.resolver.catalog_membership import StaticCatalogMembershipReader, TtlCatalogMembershipReader

from test_metrics_cache import BRAND_CARDS, CACHE_BRANDS


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
    return MarketScopeResolver(cache_reader=cache_reader, cd_mart_reader=_cd_mart_reader())


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


def test_detect_market_scope_intent_recognizes_explicit_strategic_metric() -> None:
    intent = detect_market_scope_intent("아일리아 시장 HHI")

    assert intent is not None
    assert intent.brand_hint == "아일리아"
    assert intent.metric == "hhi"
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


def test_market_scope_fixture_uses_cache_without_cause_dependency() -> None:
    resolver = MarketScopeResolver(
        cache_reader=StaticMetricsCacheReader(cache_brands=CACHE_BRANDS, market_status=BRAND_CARDS),
        cd_mart_reader=_cd_mart_reader(),
    )

    result = resolver.answer("리바로랑 같은 시장 매출", view_type="market_landscape")

    assert result["tool_calls"][0]["render_data"]["market_size_recent_krw"] == 225_677_368_890.97986


def test_market_scope_mart_failure_returns_typed_unavailable_without_cause_trace() -> None:
    class BrokenQueryLayer:
        def market_scope(self, brand: str):
            raise LookupError("injected mart failure")

    resolver = MarketScopeResolver(
        cache_reader=StaticMetricsCacheReader(cache_brands=CACHE_BRANDS, market_status=BRAND_CARDS),
        query_layer=BrokenQueryLayer(),  # type: ignore[arg-type]
    )

    result = resolver.answer("리바로 시장 규모", view_type="market_landscape")

    call = result["tool_calls"][0]
    assert call["render_data"]["status"] == "query_failed"
    assert call["qa_trace"]["status"] == "query_failed"
    assert "endpoint" not in call["qa_trace"]
    assert result["router_diagnostics"]["gate"] == "typed_unavailable"
    assert "수치를 추정하지 않습니다" in result["answer"]
    assert "연속 상승" not in result["answer"]
    assert "연속 하락" not in result["answer"]


def test_market_member_mapping_failure_is_typed_without_generic_query_error() -> None:
    class MissingMemberQueryLayer:
        def market_members(self, *args, **kwargs):
            raise LookupError("mart market not found: market=ml_003")

    memberships = TtlCatalogMembershipReader(
        StaticCatalogMembershipReader(
            ({"brand": "마운자로", "market_id": "ml_003", "market_name": "당뇨 시장"},)
        ),
        ttl_seconds=300,
    )
    resolver = MarketScopeResolver(
        cache_reader=StaticMetricsCacheReader(cache_brands=[], market_status={}),
        membership_reader=memberships,
        query_layer=MissingMemberQueryLayer(),  # type: ignore[arg-type]
    )

    result = resolver.answer("마운자로 시장에 있는 브랜드 알려줘", view_type="market_landscape")

    assert result["router_diagnostics"]["gate"] == "typed_unavailable"
    assert result["router_diagnostics"]["gate_reason"] == "market_members_mapping_unavailable"
    assert "시장 매핑이 확인되지 않습니다" in result["answer"]
    assert "조회 오류" not in result["answer"]


def test_named_market_member_count_is_forwarded_as_a_bounded_limit() -> None:
    class RecordingMemberQueryLayer:
        def __init__(self) -> None:
            self.limits: list[int] = []

        def market_members(
            self,
            brand: str = "",
            *,
            market: str | None = None,
            period: str = "latest",
            limit: int = 20,
            include_other: bool = False,
        ) -> dict:
            self.limits.append(limit)
            return {
                "source": "UBIST",
                "tool": "get_market_members",
                "render_data": {
                    "status": "ok",
                    "market": market,
                    "market_id": market,
                    "market_name": "고지혈증 시장",
                    "period": "2026-05",
                    "member_brands": tuple(f"브랜드{index}" for index in range(1, limit + 1)),
                    "displayed_brand_count": limit,
                    "total_brands_in_market": 555,
                    "source_label": "UBIST",
                },
            }

    memberships = TtlCatalogMembershipReader(
        StaticCatalogMembershipReader(
            ({"brand": "리바로", "market_id": "ml_006", "market_name": "고지혈증 시장"},)
        ),
        ttl_seconds=300,
    )
    query_layer = RecordingMemberQueryLayer()
    resolver = MarketScopeResolver(
        cache_reader=StaticMetricsCacheReader(cache_brands=[], market_status={}),
        membership_reader=memberships,
        query_layer=query_layer,  # type: ignore[arg-type]
    )

    result = resolver.answer_named_market("고지혈증 시장 브랜드 50개")

    assert query_layer.limits == [20]
    data = result["tool_calls"][0]["render_data"]
    assert data["total_brands_in_market"] == 555
    assert data["displayed_brand_count"] == 20
    assert data["requested_limit"] == 50
    assert data["limit_capped"] is True
    assert "총 555개 중 20개 표시" in result["answer"]
    assert "표시 상한 20개" in result["answer"]


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


def test_strategic_metric_for_general_only_brand_is_typed_as_market_unavailable() -> None:
    class ExplodingQueryLayer:
        def market_scope(self, brand: str):
            raise AssertionError(f"general-only brand must not enter strategic query layer: {brand}")

    memberships = TtlCatalogMembershipReader(
        StaticCatalogMembershipReader(
            (
                {
                    "brand": "아일리아",
                    "market_id": "",
                    "market_name": "",
                    "support_source": "general_mart",
                },
            )
        ),
        ttl_seconds=300,
    )
    resolver = MarketScopeResolver(
        cache_reader=StaticMetricsCacheReader(cache_brands=[], market_status={}),
        membership_reader=memberships,
        query_layer=ExplodingQueryLayer(),
    )

    result = resolver.answer("아일리아 전략뷰 HHI", view_type="market_landscape")

    assert resolver.is_general_only_brand("아일리아 전략뷰 HHI") is True
    assert result["router_diagnostics"]["gate"] == "typed_unavailable"
    assert result["sources"] == ["strategic_market_not_member"]
    assert "전략시장 정의에 포함되지 않아" in result["answer"]
    assert "브랜드를 확인" not in result["answer"]


def test_strategic_other_member_listing_uses_full_population_and_excludes_top_five() -> None:
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

    records = tuple(record("리바로" if rank == 1 else f"브랜드{rank}", 10_000.0 - rank) for rank in range(1, 9))
    resolver = MarketScopeResolver(
        cache_reader=StaticMetricsCacheReader(cache_brands=CACHE_BRANDS, market_status={}),
        query_layer=StrategicQueryLayer(reader=StaticStrategicMartReader(records)),
    )

    result = resolver.answer(
        "리바로 경쟁 순위 '기타'에 포함된 제품 목록",
        view_type="market_landscape",
    )

    data = result["tool_calls"][0]["render_data"]
    assert result["tool_calls"][0]["status"] == "ok"
    assert data["status"] == "ok"
    assert data["total_brands_in_market"] == 8
    assert data["displayed_brand_count"] == 3
    assert [row["brand"] for row in data["level_segments"]] == ["브랜드6", "브랜드7", "브랜드8"]
    assert "총 8개 중 3개 표시" in result["answer"]


def test_named_strategic_market_member_listing_needs_no_anchor_brand() -> None:
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

    resolver = MarketScopeResolver(
        cache_reader=StaticMetricsCacheReader(cache_brands=CACHE_BRANDS, market_status={}),
        query_layer=StrategicQueryLayer(
            reader=StaticStrategicMartReader(
                (record("리바로", 10.0), record("로수젯", 20.0), record("리피토", 15.0))
            )
        ),
    )

    result = resolver.answer_named_market("고지혈증 시장에 어떤 브랜드들이 있어?")

    data = result["tool_calls"][0]["render_data"]
    assert result["resolution"]["market_id"] == "ml_006"
    assert "고지혈증" in result["resolution"]["market_name"]
    assert "고지혈증" in data["market_name"]
    assert data["total_brands_in_market"] == 3
    assert data["displayed_brand_count"] == 3
    assert "고지혈증" in result["answer"]
    assert "총 3개 중 3개 표시" in result["answer"]


def test_explicit_market_id_member_listing_uses_requested_period() -> None:
    def record(brand: str, old_value: float, latest_value: float) -> MartRecord:
        return MartRecord(
            ml_id="ml_006",
            brand_name=brand,
            source="ubist",
            measure="sales",
            metric_history={
                "2025-04": {"raw_value": old_value},
                "2026-05": {"raw_value": latest_value},
            },
            channel_data={},
            specialty_data={},
            dimension_data={},
            by_dimension={},
        )

    resolver = MarketScopeResolver(
        cache_reader=StaticMetricsCacheReader(cache_brands=CACHE_BRANDS, market_status={}),
        query_layer=StrategicQueryLayer(
            reader=StaticStrategicMartReader(
                (record("과거선두", 9.0, 1.0), record("현재선두", 1.0, 9.0))
            )
        ),
    )

    result = resolver.answer_market_id(
        "ml_006 2025년 4월 시장에 어떤 브랜드들이 있어?",
        market_id="ml_006",
        period="2025-04",
    )

    call = result["tool_calls"][0]
    assert call["tool"] == "get_market_members"
    assert call["render_data"]["period"] == "2025-04"
    assert call["render_data"]["member_brands"] == ("과거선두", "현재선두")


def test_named_market_member_listing_rejects_missing_explicit_period() -> None:
    def record(brand: str) -> MartRecord:
        return MartRecord(
            ml_id="ml_006",
            brand_name=brand,
            source="ubist",
            measure="sales",
            metric_history={"2026-05": {"raw_value": 1.0}},
            channel_data={},
            specialty_data={},
            dimension_data={},
            by_dimension={},
        )

    resolver = MarketScopeResolver(
        cache_reader=StaticMetricsCacheReader(cache_brands=CACHE_BRANDS, market_status={}),
        query_layer=StrategicQueryLayer(reader=StaticStrategicMartReader((record("리바로"),))),
    )

    result = resolver.answer_named_market("고지혈증 시장의 2024년 브랜드 목록")

    assert result["tool_calls"][0]["tool"] == "query_failed"
    assert "2026-05" not in result["answer"]


def test_monthly_market_golden_uses_mart_without_touching_backend() -> None:
    class DualTruthQueryLayer(StrategicQueryLayer):
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def market_scope(self, brand: str) -> dict:
            self.calls.append(("backend", brand))
            return _scope_call(hhi=262.4174, source="backend_api")

        def market_scope_from_mart(self, brand: str) -> dict:
            self.calls.append(("mart", brand))
            return _scope_call(hhi=253.6207, source="UBIST")

    query_layer = DualTruthQueryLayer()
    resolver = MarketScopeResolver(
        cache_reader=StaticMetricsCacheReader(cache_brands=CACHE_BRANDS, market_status=BRAND_CARDS),
        query_layer=query_layer,
    )

    result = resolver.answer_monthly_market_golden(
        "고지혈증 시장 HHI",
        anchor_brand="리바로",
    )

    data = result["tool_calls"][0]["render_data"]
    assert query_layer.calls == [("mart", "리바로")]
    assert data["hhi_recent"] == pytest.approx(253.6207)
    assert "262.42" not in result["answer"]
    assert "연속 상승" not in result["answer"]
    assert "연속 하락" not in result["answer"]
    assert result["router_diagnostics"]["gate_reason"] == "monthly_market_golden"


def test_explicit_brand_market_scope_keeps_backend_truth() -> None:
    class DualTruthQueryLayer(StrategicQueryLayer):
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def market_scope(self, brand: str) -> dict:
            self.calls.append(("backend", brand))
            return _scope_call(hhi=262.4174, source="backend_api")

        def market_scope_from_mart(self, brand: str) -> dict:
            self.calls.append(("mart", brand))
            return _scope_call(hhi=253.6207, source="UBIST")

    query_layer = DualTruthQueryLayer()
    resolver = MarketScopeResolver(
        cache_reader=StaticMetricsCacheReader(cache_brands=CACHE_BRANDS, market_status=BRAND_CARDS),
        query_layer=query_layer,
    )

    result = resolver.answer("리바로가 속한 시장 HHI", view_type="market_landscape")

    assert query_layer.calls == [("backend", "리바로")]
    assert result["tool_calls"][0]["render_data"]["hhi_recent"] == pytest.approx(262.4174)


def _scope_call(*, hhi: float, source: str) -> dict:
    return {
        "source": source,
        "tool": "get_market_landscape",
        "summary_text": "리바로 시장 범위를 조회했습니다.",
        "render_data": {
            "market": "ml_006",
            "market_name": "고지혈증 시장",
            "scope": "market",
            "scope_label": "시장 전체",
            "period": "2026-05",
            "anchor_brand": "리바로",
            "total_brands_in_market": 555,
            "market_size_recent_krw": 213_925_043_319.36,
            "market_size_억원": 2_139.2504331936,
            "hhi_recent": hhi,
            "level_segments": [
                {
                    "rank": 1,
                    "brand": "로수젯",
                    "ms_recent_pct": 9.12649,
                    "value": 19_523_856_225.95,
                },
                {
                    "rank": 2,
                    "brand": "리피토",
                    "ms_recent_pct": 6.12777,
                    "value": 13_108_840_203.03,
                },
            ],
            "source_label": source,
        },
    }


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
