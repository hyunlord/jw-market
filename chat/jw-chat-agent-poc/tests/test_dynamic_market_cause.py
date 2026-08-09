from __future__ import annotations

from dataclasses import dataclass

from jw_chat_agent_poc.service import app as service_app
from jw_chat_agent_poc.service.general_view_routing import (
    _asks_dynamic_cause_analysis,
    _general_view_projection,
)
from jw_chat_agent_poc.tools.general_view_backend import GeneralMarket, TopBrand
from jw_chat_agent_poc.tools.metrics.market_scope import MarketScopeResolver


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


def test_strategic_cause_survives_binding_and_renders_chart() -> None:
    resolver = MarketScopeResolver.__new__(MarketScopeResolver)
    resolver._resolver = _StrategicResolver()
    resolver._query_layer = _StrategicQueryLayer()
    resolver._general_view = _GeneralView()
    question = "리바로 원인분석 좀 뽑아줘"

    final = service_app._compute_final_answer(
        question,
        resolver.answer_cause_analysis(question),
        "cause-binding",
    )

    assert "| 순위 | 브랜드 |" in final.text
    assert final.charts


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
