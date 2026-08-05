from __future__ import annotations

from jw_chat_agent_poc.tool_use.catalog import TOOL_DESCRIPTION_CATALOG
from jw_chat_agent_poc.tool_use.v3_execution_contracts import MarketMetricFact
from jw_chat_agent_poc.tool_use.v3_fusion_semantics import claim_semantic_rejection
from jw_chat_agent_poc.tool_use.market_scope_contract import MarketScope, MarketScopeKind, ScopeResolution
from jw_chat_agent_poc.tool_use.market_scope_projection import general_scope_result
from jw_chat_agent_poc.tool_use.v3_selection import selection_tool_specs
from jw_chat_agent_poc.tools.general_view_backend import parse_general_market_response
from jw_chat_agent_poc.tools.deep_analysis_backend import project_deep_analysis_response


def _market_fact(raw_result: dict[str, object]) -> MarketMetricFact:
    return MarketMetricFact(
        evidence_id="v3-shadow:market.get_deep_analysis:test",
        tool_name="market.get_deep_analysis",
        arguments={"brand": "리바로"},
        raw_result=raw_result,
        missing_required_fields=(),
        entity="리바로",
        metric="forecast",
        period="2027",
        unit="KRW",
        view="general",
        market="C10A1",
    )


def _general_payload() -> dict[str, object]:
    return {
        "result": {
            "unit_label": "KRW",
            "market_meta": {
                "market_definition_label": "ATC4 C10A1",
                "filters": {
                    "view": "general",
                    "atc4": ["C10A1"],
                    "source": "ubist",
                    "measure": "sales",
                },
            },
            "data": {
                "kpi": {"market_size_recent": 120.0},
                "sources_data": {
                    "market_size_series": [
                        {"period": "2025", "value": 100.0},
                        {"period": "2026", "value": 120.0},
                    ]
                },
                "hhi_series_5y": [
                    {"period": "2025", "hhi": 3000.12344},
                    {"period": "2026", "hhi": 3188.040362260885},
                ],
                "brand_ranking_stacked": {
                    "years": [2024, 2025, 2026],
                    "yearly": [
                        {"year": 2024, "rankings": [{"brand": "리바로", "rank": 3, "value": 0.0, "ms_pct": 0.0}]},
                        {"year": 2025, "rankings": [{"brand": "리바로", "rank": 2, "value": 20.0, "ms_pct": 20.0}]},
                        {"year": 2026, "rankings": [{"brand": "리바로", "rank": 1, "value": 25.0, "ms_pct": 25.0}]},
                    ],
                },
                "company_ranking_stacked": {
                    "years": [2026],
                    "yearly": [
                        {"year": 2026, "rankings": [{"company": "JW중외제약", "rank": 1, "value": 30.0, "ms_pct": 30.0}]}
                    ],
                },
                "target_customer_competition_by_channel": {
                    "views": [
                        {
                            "target_name": "종합병원",
                            "periods": ["2025", "2026"],
                            "trend_brands": [
                                {
                                    "brand": "리바로",
                                    "value_series": [0.0, 25.0],
                                    "ms_series": [0.0, 25.0],
                                    "rank_series": [3, 1],
                                }
                            ],
                        }
                    ],
                },
                "ei_ms_matrix": {"data": []},
            },
        }
    }


def test_deep_analysis_catalog_uses_the_observed_api_contract() -> None:
    records = {record.name: record for record in TOOL_DESCRIPTION_CATALOG}
    specs = {spec.name: spec for spec in selection_tool_specs()}

    record = records["market.get_deep_analysis"]
    schema = specs["market.get_deep_analysis"].input_model.model_json_schema()

    assert "시스템 예측" in record.catalog_description
    assert "의사결정 배경" in record.catalog_description
    assert set(schema["properties"]) == {
        "brand",
        "view_kind",
        "market_id",
        "source",
        "view",
        "scope",
    }
    assert schema["required"] == ["brand", "view_kind", "market_id", "source"]


def test_forecast_claim_requires_system_forecast_label() -> None:
    fact = _market_fact(
        {
            "value_kind": "system_forecast",
            "period": "2027",
            "value": 130.0,
        }
    )

    assert claim_semantic_rejection("2027년 매출은 130억원입니다.", (fact,)) == (
        "forecast_label_missing"
    )
    assert claim_semantic_rejection(
        "시스템 예측에 따르면 2027년 매출은 130억원입니다.", (fact,)
    ) is None


def test_observed_and_forecast_values_cannot_share_one_claim() -> None:
    observed = _market_fact(
        {"value_kind": "observed", "period": "2026", "value": 120.0}
    )
    forecast = _market_fact(
        {"value_kind": "system_forecast", "period": "2027", "value": 130.0}
    )

    assert claim_semantic_rejection(
        "실적은 120억원이고 시스템 예측은 130억원입니다.",
        (observed, forecast),
    ) == "observed_forecast_mixed_claim"


def test_general_series_preserves_period_aligned_dashboard_inputs() -> None:
    market = parse_general_market_response(
        _general_payload(),
        requested_atc4="C10A1",
        requested_source="ubist",
        requested_measure="sales",
    )

    assert market.hhi_series == (
        ("2025", 3000.12344),
        ("2026", 3188.040362260885),
    )
    assert market.company_ranking_series[0]["period"] == "2026"
    assert market.market_share_trajectory[-1]["ms"] == 25.0
    assert market.customer_competition_trend["views"][0]["periods"] == ["2025", "2026"]
    assert market.market_size_period == market.hhi_period == "2026"
    projected = general_scope_result(
        "market.get_market_size",
        market,
        ScopeResolution(
            scope=MarketScope(kind=MarketScopeKind.GENERAL_ATC4, atc4=("C10A1",)),
            source="ubist",
            normalized_arguments={},
        ),
    )
    table_names = {
        table["name"] for table in projected["render_data"]["dashboard_tables"]
    }
    assert {
        "시장 규모 및 성장 추이",
        "HHI 추이",
        "브랜드 점유율 및 순위 추이",
        "회사 경쟁 순위 추이",
        "Top5 고객 경쟁 추이",
    } <= table_names
    share_table = next(
        table
        for table in projected["render_data"]["dashboard_tables"]
        if table["name"] == "브랜드 점유율 및 순위 추이"
    )
    assert share_table["rows"][0][2] == 0.0
    customer_table = next(
        table
        for table in projected["render_data"]["dashboard_tables"]
        if table["name"] == "Top5 고객 경쟁 추이"
    )
    assert customer_table["rows"][0][3:5] == (0.0, 0.0)


def test_simulation_claim_requires_system_simulation_label() -> None:
    fact = _market_fact(
        {
            "value_kind": "system_simulation",
            "period": "2027",
            "value": 128.0,
        }
    )

    assert claim_semantic_rejection("2027년 매출은 128억원입니다.", (fact,)) == (
        "simulation_label_missing"
    )
    assert claim_semantic_rejection(
        "시스템 시뮬레이션에서 2027년 매출은 128억원입니다.", (fact,)
    ) is None


def test_deep_analysis_projection_keeps_api_values_and_withholds_ai_prose() -> None:
    payload = {
        "generated_at": "2026-08-05T12:00:00+09:00",
        "data": {
            "forecast": {
                "by_combo": {
                    "ubist.amount": {
                        "history_periods": ["2026"],
                        "forecast_periods": ["2027"],
                        "brands": [
                            {
                                "brand": "리바로",
                                "history_values": [120.0],
                                "history_ms_pct": [24.0],
                                "forecast_values": [130.0],
                                "forecast_ms_pct": [25.0],
                            }
                        ]
                    },
                    "iqvia.amount": {
                        "forecast_periods": ["2027"],
                        "brands": [
                            {
                                "brand": "다른소스",
                                "forecast_values": [999.0],
                                "forecast_ms_pct": [99.0],
                            }
                        ],
                    }
                }
            },
            "simulation": {
                "by_combo": {
                    "ubist.amount": {
                        "by_brand": {
                            "리바로": {
                                "forecast_periods": ["2027"],
                                "scenarios": {
                                    "base": {"values": [128.0]}
                                }
                            }
                        }
                    }
                }
            },
            "brand_factors": {
                "ubist": [{"brand": "리바로", "factors": {"seller": "JW"}}],
                "iqvia": [{"brand": "다른소스", "factors": {"seller": "OTHER"}}],
            },
            "ai_analysis": "매출이 성장할 것입니다.",
        },
    }

    result = project_deep_analysis_response(
        payload,
        brand="리바로",
        view_kind="general",
        market_id="C10A1",
        source="ubist",
    )

    evidence = result["evidence"]
    assert isinstance(evidence, tuple)
    assert {item["value_kind"] for item in evidence} == {
        "observed",
        "system_forecast",
        "system_simulation",
        "observed_profile",
    }
    forecast = next(item for item in evidence if item["value_kind"] == "system_forecast")
    assert forecast["value"] == 130.0
    assert all(item.get("brand") != "다른소스" for item in evidence)
    table_names = {
        table["name"] for table in result["render_data"]["dashboard_tables"]
    }
    assert table_names == {
        "심층분석 실적",
        "시스템 예측",
        "시스템 시뮬레이션",
        "브랜드 프로파일링",
    }
    observed_table = next(
        table
        for table in result["render_data"]["dashboard_tables"]
        if table["name"] == "심층분석 실적"
    )
    forecast_table = next(
        table
        for table in result["render_data"]["dashboard_tables"]
        if table["name"] == "시스템 예측"
    )
    assert observed_table["rows"][0][4] == 120.0
    assert forecast_table["rows"][0][4] == 130.0
    assert result["model_insight_status"] == "available_model_generated"
    assert result["insight"] == {
        "raw_text": "매출이 성장할 것입니다.",
        "generated_by": "deep-analysis-api-llm",
        "target_market": "C10A1",
        "target_brand": "리바로",
        "api_response_location": "data.ai_analysis",
        "fetched_at_utc": result["insight"]["fetched_at_utc"],
    }
    assert str(result["insight"]["fetched_at_utc"]).endswith("Z")
