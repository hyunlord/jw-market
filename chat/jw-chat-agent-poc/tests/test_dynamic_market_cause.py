from __future__ import annotations

from dataclasses import dataclass, replace

from jw_chat_agent_poc.service import app as service_app
from jw_chat_agent_poc.service.charts import filter_charts_for_binding
from jw_chat_agent_poc.service.evidence_binding import evidence_facts_from_result
from jw_chat_agent_poc.service.general_view_routing import (
    _asks_dynamic_cause_analysis,
    _general_view_projection,
)
from jw_chat_agent_poc.tools.general_view_backend import GeneralMarket, TopBrand
from jw_chat_agent_poc.tools.metrics.market_scope import MarketScopeResolver, _strategic_cause_result


def _general_market() -> GeneralMarket:
    return GeneralMarket(
        view_type="general_view",
        market_basis="ATC4",
        atc4_code="S01P0",
        atc4_description="안과용 혈관신생 억제제",
        source="UBIST",
        measure="sales",
        unit="KRW",
        period="2026-06",
        market_size=100_000_000_000,
        brand="아일리아",
        brand_value=42_000_000_000,
        brand_share_pct=42.0,
        brand_rank=1,
        top_brands=(
            TopBrand("아일리아", 42_000_000_000, 42.0, 1),
            TopBrand("루센티스", 24_000_000_000, 24.0, 2),
        ),
        market_size_series=(("2026-05", 96_000_000_000), ("2026-06", 100_000_000_000)),
        hhi_recent=2460.0,
        hhi_series=(("2026-05", 2390.0), ("2026-06", 2460.0)),
        selected_data_path="direct_mart",
    )


def test_cause_question_is_recognized_before_external_lookup() -> None:
    assert _asks_dynamic_cause_analysis("리바로 원인분석 좀 뽑아줘")
    assert _asks_dynamic_cause_analysis("고지혈증 시장 원인 분석")
    assert not _asks_dynamic_cause_analysis("리바로 관련 최근 이슈 알려줘")


def test_general_cause_projection_contains_table_and_chart_payload() -> None:
    projection = _general_view_projection(_general_market(), "아일리아 원인분석")

    assert projection is not None
    intent, _question, data, charts = projection
    assert intent == "CAUSE_ANALYSIS"
    assert data["dashboard_tables"]
    assert charts
    assert charts == data["chart_payloads"]


def test_general_cause_chart_references_resolve_to_numeric_evidence() -> None:
    market = replace(
        _general_market(),
        dashboard_tables=(
            {
                "name": "성장 기여",
                "columns": ("브랜드", "성장 기여", "기여율(%)"),
                "rows": (("아일리아", 12_345.0, 67.89),),
            },
        ),
    )
    projection = _general_view_projection(market, "아일리아 원인분석")
    assert projection is not None
    _intent, _question, data, charts = projection
    result = {
        "cause_analysis_ready": True,
        "tool_calls": [
            {
                "tool": "general_view_dynamic_market",
                "source": "UBIST",
                "render_data": data,
            }
        ],
    }
    paths = {fact.path for fact in evidence_facts_from_result(result)}
    refs = {reference for chart in charts for reference in chart["evidence_refs"]}

    assert refs
    assert refs <= paths
    for table_index, table in enumerate(data["dashboard_tables"]):
        for row_index, row in enumerate(table["rows"]):
            for column_index, value in enumerate(row):
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    assert (
                        f"render_data.dashboard_tables[{table_index}]"
                        f".rows[{row_index}][{column_index}]"
                    ) in paths
    assert filter_charts_for_binding(charts, result=result, question="아일리아 원인분석") == charts


@dataclass
class _Resolution:
    canonical_brand: str
    market_ids: tuple[str, ...]
    market_id: str | None = None
    market_name: str | None = None


class _StrategicResolver:
    def resolve(self, _question: str, allow_default: bool = False) -> _Resolution:
        assert allow_default is False
        return _Resolution("리바로", ("ml_006",), "ml_006", "고지혈증")

    def explicit_market(self, _question: str):
        return None


class _StrategicQueryLayer:
    def market_scope_from_mart(self, brand: str, market: str | None = None):
        assert brand == "리바로"
        assert market is None
        return {
            "source": "UBIST",
            "tool": "get_market_landscape",
            "summary_text": "전략 mart 조회",
            "render_data": {
                "market": "ml_006",
                "market_id": "ml_006",
                "market_name": "고지혈증",
                "period": "2026-06",
                "market_size_recent_krw": 200_000_000_000,
                "market_size_억원": 2_000.0,
                "hhi_recent": 1800.0,
                "level_segments": (
                    {
                        "brand": "리피토",
                        "value": 40_000_000_000,
                        "value_억원": 400.0,
                        "ms_recent_pct": 20.0,
                        "rank": 1,
                    },
                    {
                        "brand": "리바로",
                        "value": 30_000_000_000,
                        "value_억원": 300.0,
                        "ms_recent_pct": 15.0,
                        "rank": 2,
                    },
                ),
                "source_label": "UBIST",
            },
        }

    def market_scope_by_id(self, market_id: str, period: str, *, market_display_name: str):
        assert market_id == "ml_006"
        assert period == "latest"
        assert market_display_name == "고지혈증"
        return self.market_scope_from_mart("리바로")

    def cause_card_data(self, anchor_brand: str, market: str) -> dict[str, object]:
        assert anchor_brand in {"리바로", "리피토"}
        assert market == "ml_006"
        return {
            "company_ranking_series": (
                {"rank": 1, "name": "A사", "value_recent_억원": 700.0, "ms_recent_pct": 35.0},
                {"rank": 2, "name": "B사", "value_recent_억원": 500.0, "ms_recent_pct": 25.0},
            ),
            "ei_ms": {"brand": "리바로", "ei": 120.0, "ms_recent_pct": 15.0},
            "growth_contribution": {
                "brand": "리바로",
                "growth_contribution_pct": 12.5,
                "ms_recent_pct": 15.0,
                "growth_contribution_period_start": "2025-06",
                "growth_contribution_period_end": "2026-06",
            },
            "analysis_level_trend": (
                {"name": "스타틴", "from_period": "2025-09", "from_ms_pct": 80.0, "to_period": "2026-06", "to_ms_pct": 82.0},
            ),
            "customer_competition": (
                {"name": "의원", "from_period": "2025-09", "from_ms_pct": 55.0, "to_period": "2026-06", "to_ms_pct": 58.0},
            ),
        }


class _GeneralView:
    def answer(self, *_args, **_kwargs):
        raise AssertionError("strategic brand must not use the general view")


def test_strategic_cause_uses_mart_and_returns_table_and_chart() -> None:
    resolver = MarketScopeResolver.__new__(MarketScopeResolver)
    resolver._resolver = _StrategicResolver()
    resolver._query_layer = _StrategicQueryLayer()
    resolver._general_view = _GeneralView()

    result = resolver.answer_cause_analysis("리바로 원인분석 좀 뽑아줘")

    assert result["cause_analysis_ready"] is True
    assert result["resolution"]["market_id"] == "ml_006"
    assert "| 순위 | 브랜드 |" in result["answer"]
    assert result["tool_calls"][0]["render_data"]["chart_payloads"]


def test_strategic_cause_projects_only_cards_backed_by_mart_data() -> None:
    resolver = MarketScopeResolver.__new__(MarketScopeResolver)
    resolver._resolver = _StrategicResolver()
    resolver._query_layer = _StrategicQueryLayer()
    resolver._general_view = _GeneralView()

    result = resolver.answer_cause_analysis("리바로 원인분석 좀 뽑아줘")
    data = result["tool_calls"][0]["render_data"]
    table_names = {table["name"] for table in data["dashboard_tables"]}

    assert {
        "회사 순위",
        "회사 집중도",
        "EI & MS",
        "성장기여 & MS",
        "분석레벨별 추세",
        "Waterfall",
        "고객별 경쟁구도",
    } <= table_names
    assert all(data["cause_card_support"][key] for key in (
        "A4_company_ranking",
        "A5_company_concentration",
        "B1_ei_ms",
        "B2_growth_contribution_ms",
        "C1_analysis_level_trend",
        "D1_waterfall",
        "D2_customer_competition",
    ))


def test_strategic_cause_does_not_claim_cards_without_mart_data() -> None:
    call = _StrategicQueryLayer().market_scope_from_mart("리바로")

    result = _strategic_cause_result(
        "리바로 원인분석",
        call,
        market_name="고지혈증",
        brand="리바로",
    )

    support = result["tool_calls"][0]["render_data"]["cause_card_support"]
    assert not any(support[key] for key in (
        "A4_company_ranking",
        "A5_company_concentration",
        "B1_ei_ms",
        "B2_growth_contribution_ms",
        "C1_analysis_level_trend",
        "D1_waterfall",
        "D2_customer_competition",
    ))


class _NamedMarketResolver(_StrategicResolver):
    def explicit_market(self, question: str):
        assert "고지혈증 시장" in question
        return "ml_006", "고지혈증"


def test_named_market_cause_uses_catalog_market_and_mart_projection() -> None:
    resolver = MarketScopeResolver.__new__(MarketScopeResolver)
    resolver._resolver = _NamedMarketResolver()
    resolver._query_layer = _StrategicQueryLayer()
    resolver._general_view = _GeneralView()

    result = resolver.answer_cause_analysis("고지혈증 시장 원인분석")

    assert result["cause_analysis_ready"] is True
    assert result["resolution"]["market_id"] == "ml_006"
    assert result["resolution"]["market_name"] == "고지혈증"
    assert result["tool_calls"][0]["render_data"]["chart_payloads"]
    assert set(result["tool_calls"][0]["render_data"]["cause_card_support"]) == {
        "A1_market_size_growth",
        "A2_brand_ranking",
        "A3_hhi",
        "A4_company_ranking",
        "A5_company_concentration",
        "B1_ei_ms",
        "B2_growth_contribution_ms",
        "C1_analysis_level_trend",
        "D1_waterfall",
        "D2_customer_competition",
        "D3_level_top5",
    }


def test_strategic_cause_survives_binding_and_renders_chart() -> None:
    resolver = MarketScopeResolver.__new__(MarketScopeResolver)
    resolver._resolver = _StrategicResolver()
    resolver._query_layer = _StrategicQueryLayer()
    resolver._general_view = _GeneralView()
    question = "리바로 원인분석 좀 뽑아줘"

    call = resolver._query_layer.market_scope_from_mart("리바로")
    call["source"] = "mariadb"
    call["render_data"]["level_segments"][0]["value_억원"] = 400.12345
    call["render_data"]["level_segments"][0]["ms_recent_pct"] = 20.12345
    result = _strategic_cause_result(question, call, market_name="고지혈증", brand="리바로")
    final = service_app.compute_final_answer(
        question,
        result,
        "cause-binding",
    )

    assert "| 순위 | 브랜드 |" in final.text
    assert "| 1 | 리피토 | 400.12 | 20.12 |" in final.text
    assert "근거 payload에 없는 수치는 출력에서 제외했습니다." not in final.text
    assert final.charts


def test_strategic_cause_exposes_every_rendered_segment_number_as_evidence() -> None:
    resolver = MarketScopeResolver.__new__(MarketScopeResolver)
    resolver._resolver = _StrategicResolver()
    resolver._query_layer = _StrategicQueryLayer()
    resolver._general_view = _GeneralView()

    result = resolver.answer_cause_analysis("리바로 원인분석 좀 뽑아줘")
    paths = {fact.path for fact in evidence_facts_from_result(result)}

    assert "render_data.level_segments[0].rank" in paths
    assert "render_data.level_segments[0].value_억원" in paths
    assert "render_data.level_segments[0].ms_recent_pct" in paths
    assert "render_data.level_segments[1].value_억원" in paths
    assert "render_data.level_segments[1].ms_recent_pct" in paths


def test_cause_chart_fails_closed_when_an_evidence_reference_is_missing() -> None:
    chart = {
        "type": "bar",
        "labels": ["리피토"],
        "datasets": [{"label": "매출", "data": [400.0]}],
        "evidence_refs": ["render_data.level_segments[0].missing_value"],
    }
    result = {
        "cause_analysis_ready": True,
        "tool_calls": [_StrategicQueryLayer().market_scope_from_mart("리바로")],
    }

    assert filter_charts_for_binding([chart], result=result, question="리바로 원인분석") == []


def test_partial_general_cause_chart_keeps_only_evidenced_numeric_points() -> None:
    market = replace(
        _general_market(),
        top_brands=(
            TopBrand(brand="아일리아", rank=1, value=42_000_000_000, share_pct=42.0),
            TopBrand(brand="누락", rank=2, value=1_000_000_000, share_pct=None),
            TopBrand(brand="루센티스", rank=3, value=24_000_000_000, share_pct=24.0),
        ),
    )
    projection = _general_view_projection(market, "아일리아 원인분석")
    assert projection is not None
    _intent, _question, data, charts = projection
    result = {
        "cause_analysis_ready": True,
        "tool_calls": [{"tool": "general_view_dynamic_market", "source": "UBIST", "render_data": data}],
    }
    share_chart = next(chart for chart in charts if chart["title"] == "브랜드 점유율")

    assert share_chart["labels"] == ["아일리아", "루센티스"]
    assert share_chart["datasets"][0]["data"] == [42.0, 24.0]
    assert filter_charts_for_binding(charts, result=result, question="아일리아 원인분석") == charts


def test_partial_strategic_cause_chart_keeps_only_evidenced_numeric_points() -> None:
    call = _StrategicQueryLayer().market_scope_from_mart("리바로")
    segments = list(call["render_data"]["level_segments"])
    segments.insert(1, {"brand": "누락", "value_억원": None, "ms_recent_pct": None, "rank": 2})
    call["render_data"]["level_segments"] = tuple(segments)
    result = _strategic_cause_result("리바로 원인분석", call, market_name="고지혈증", brand="리바로")
    charts = result["tool_calls"][0]["render_data"]["chart_payloads"]

    assert charts[0]["labels"] == ["리피토", "리바로"]
    assert charts[0]["datasets"][0]["data"] == [400.0, 300.0]
    assert filter_charts_for_binding(charts, result=result, question="리바로 원인분석") == charts


class _CauseResolver:
    def general_route(self, _question: str):
        raise AssertionError("cause dispatch must precede ordinary routing")

    def answer_cause_analysis(self, question: str):
        return {"answer": question, "cause_analysis_ready": True, "tool_calls": [], "sources": []}


def test_service_dispatches_cause_before_agent_and_external_cutover() -> None:
    result = service_app._answer_without_pending(
        _CauseResolver(),
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("agent must not run")),
        "cause-conversation",
        "리바로 원인분석 좀 뽑아줘",
        "live",
        None,
        service_app.SessionStore(),
    )

    assert result["cause_analysis_ready"] is True


def test_cause_ready_final_answer_bypasses_external_content_gate(monkeypatch) -> None:
    binding_calls: list[tuple[str, str]] = []

    monkeypatch.setattr(
        service_app,
        "enforce_external_content_validity",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("external gate must not run")),
    )
    monkeypatch.setattr(
        service_app,
        "_apply_evidence_binding_gate",
        lambda question, answer, _result: binding_calls.append((question, answer)) or answer,
    )
    final = service_app._compute_final_answer(
        "리바로 원인분석",
        {
            "cause_analysis_ready": True,
            "answer": "## 원인분석\n\n| 지표 | 값 |\n| --- | ---: |\n| 시장 규모 | 2,000억원 |",
            "tool_calls": [],
            "sources": ["UBIST"],
        },
        "cause-final",
    )

    assert "원인분석" in final.text
    assert binding_calls == [("리바로 원인분석", final.text)]
