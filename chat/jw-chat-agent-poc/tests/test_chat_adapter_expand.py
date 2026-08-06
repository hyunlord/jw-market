from __future__ import annotations

from copy import deepcopy

from jw_chat_agent_poc.tool_use.market_scope_contract import (
    MarketScope,
    MarketScopeKind,
    ScopeResolution,
)
from jw_chat_agent_poc.tool_use.market_scope_projection import general_scope_result
from jw_chat_agent_poc.tool_use.v3_execution_contracts import (
    MarketMetricFact,
    V3EvidenceBundle,
)
from jw_chat_agent_poc.tool_use.v3_scope_view_set import build_scope_view_set
from jw_chat_agent_poc.tools.cause_backend import (
    CauseBackendTrace,
    parse_cause_market_response,
)
from jw_chat_agent_poc.tools.general_view_backend import parse_general_market_response

def _analysis_blocks() -> dict[str, object]:
    return {
        "analysis_levels": {
            "period_unit": "monthly",
            "channels": ["전체"],
            "levels": ["Class"],
            "periods_monthly": ["2026-04", "2026-05"],
            "periods_quarterly": [],
            "data": {
                "Class": {
                    "by_channel": {
                        "전체": [
                            {
                                "name": "Class 2",
                                "recent_share_pct": 12.0,
                                "series_pct": [11.0, 12.0],
                                "value_series": [10_000_000_000, 12_000_000_000],
                            }
                        ]
                    }
                }
            },
        },
        "analysis_level_market_status": {
            "period_unit": "monthly",
            "channels": ["순환기내과"],
            "levels": ["Class"],
            "periods_monthly": ["2026-04", "2026-05"],
            "periods_quarterly": [],
            "data": {
                "Class": {
                    "by_channel": {
                        "순환기내과": [
                            {
                                "name": "Class 2",
                                "recent_share_pct": None,
                                "series_pct": [7.9, 8.1],
                                "value_series": [7_500_000_000, 8_100_000_000],
                            }
                        ]
                    }
                }
            },
        },
        "level_top5_trend": {
            "available_levels": ["Brand", "Class"],
            "default_level": "Brand",
            "by_level": {
                "Class": {
                    "periods_10pt": ["2026-04", "2026-05"],
                    "values": [
                        {
                            "name": "Class 2",
                            "brands_in_value": [
                                {
                                    "brand": "리바로",
                                    "rank": 1,
                                    "value_series_10pt": [7_500_000_000, 8_100_000_000],
                                    "ms_series_10pt": [7.9, 8.1],
                                }
                            ],
                        }
                    ],
                }
            },
        },
    }


def _cause_payload() -> dict[str, object]:
    return {
        "brand": "리바로",
        "brand_name": "리바로",
        "source": "UBIST",
        "measure": "sales",
        "market_meta": {"market_definition_label": "고지혈증"},
        "data": {
            "kpi": {
                "market_size_recent": 213_925_043_319.3602,
                "target_brand": "리바로",
                "target_rank": 6,
                "target_share_pct": 3.7577,
                "brand_value_recent": 8_038_598_793.61,
            },
            "sources_data": {
                "market_size_series": [
                    {"period": "2026-04", "value": 226_577_368_890.98},
                    {"period": "2026-05", "value": 213_925_043_319.3602},
                ]
            },
        },
    }


def _general_payload() -> dict[str, object]:
    return {
        "status": "SUCCESS",
        "result": {
            "unit_label": "KRW",
            "market_meta": {
                "market_definition_label": "동적 시장: ATC4 C10A1",
                "filters": {
                    "view": "general",
                    "atc4": ["C10A1"],
                    "source": "ubist",
                    "measure": "sales",
                },
            },
            "data": {
                "kpi": {
                    "market_size_recent": 100_000_000_000,
                    "target_brand": "리바로",
                    "brand_value_recent": 8_000_000_000,
                    "target_share_pct": 8.0,
                    "target_rank": 2,
                },
                "sources_data": {
                    "market_size_series": [
                        {"period": "2026-04", "value": 100_000_000_000}
                    ]
                },
                "brand_ranking": {
                    "yearly": [
                        {
                            "year": 2026,
                            "rankings": [
                                {
                                    "brand": "리바로",
                                    "rank": 2,
                                    "value": 8_000_000_000,
                                    "ms_pct": 8.0,
                                }
                            ],
                        }
                    ]
                },
            },
        },
    }


def _trajectory_rows() -> dict[str, object]:
    return {
        "data": [
            {
                "brand": "리바로",
                "rank": 6,
                "value_recent": 8_038_598_793.61,
                "share_pct": 3.7577,
                "ei": 41.6922,
                "ei_5y": 41.6922,
                "cagr_5y_pct": 3.9056,
                "brand_cagr_pct": 3.9056,
                "market_cagr_pct": 9.3677,
                "momentum_score": -0.0165,
                "ei_basis": "endpoint_5y",
                "period_years": 5,
                "ei_period_years": 5,
                "brand_start_period": "2021-05",
                "brand_end_period": "2026-05",
                "market_start_period": "2021-05",
                "market_end_period": "2026-05",
            }
        ]
    }


def test_cause_adapter_preserves_cagr_analysis_trajectory_and_growth() -> None:
    payload = _cause_payload()
    payload["data"]["kpi"].update(
        {
            "market_cagr_5y_pct": 9.3677,
            "market_cagr_3y_pct": 7.1234,
            "brand_cagr_5y_pct": 3.9056,
            "brand_cagr_3y_pct": 2.3456,
            "brand_cagr_pct": 999.0,
        }
    )
    payload["data"].update(_analysis_blocks())
    payload["data"]["ei_ms_matrix"] = _trajectory_rows()
    payload["data"]["growth_contribution"] = {
        "period_start": "2025-05",
        "period_end": "2026-05",
        "by_brand": {
            "top_contributors": [
                {
                    "brand": "리바로",
                    "is_target": True,
                    "contribution": -551_800_000,
                    "contribution_pct": -0.5518,
                }
            ]
        },
    }

    market = parse_cause_market_response(
        payload,
        trace=CauseBackendTrace(
            endpoint="/api/cause/test",
            status="ok",
            latency_ms=1.0,
            cache_hit=False,
        ),
    )
    rendered = market.render_market_scope()

    assert rendered["market_cagr_5y_pct"] == 9.3677
    assert rendered["market_cagr_3y_pct"] == 7.1234
    assert rendered["brand_cagr_5y_pct"] == 3.9056
    assert rendered["brand_cagr_3y_pct"] == 2.3456
    assert rendered["analysis_levels"] == payload["data"]["analysis_levels"]
    assert rendered["analysis_level_market_status"] == payload["data"]["analysis_level_market_status"]
    assert rendered["level_top5_trend"] == payload["data"]["level_top5_trend"]
    assert rendered["brand_trajectory"][0]["momentum_score"] == -0.0165
    assert rendered["brand_trajectory"][0]["ei_basis"] == "endpoint_5y"
    assert rendered["brand_trajectory"][0]["ei_period_years"] == 5
    assert rendered["growth_contribution"]["growth_contribution_pct"] == -0.5518


def test_cause_adapter_does_not_invent_missing_three_year_cagr() -> None:
    payload = _cause_payload()
    payload["data"]["kpi"].update(
        {
            "market_cagr_5y_pct": 9.91,
            "market_cagr_3y_pct": None,
            "brand_cagr_5y_pct": 4.9408,
            "brand_cagr_3y_pct": None,
            "brand_cagr_pct": 4.9408,
        }
    )
    payload["data"]["ei_ms_matrix"] = _trajectory_rows()

    market = parse_cause_market_response(
        payload,
        trace=CauseBackendTrace(
            endpoint="/api/cause/test",
            status="ok",
            latency_ms=1.0,
            cache_hit=False,
        ),
    )
    rendered = market.render_market_scope()

    assert rendered["market_cagr_3y_pct"] is None
    assert rendered["brand_cagr_3y_pct"] is None


def test_general_adapter_projects_additive_dashboard_fields() -> None:
    payload = _general_payload()
    data = payload["result"]["data"]
    data["kpi"].update(
        {
            "market_cagr_5y_pct": 9.3677,
            "market_cagr_3y_pct": 7.1234,
            "brand_cagr_5y_pct": 3.9056,
            "brand_cagr_3y_pct": 2.3456,
        }
    )
    data.update(_analysis_blocks())
    data["ei_ms_matrix"] = _trajectory_rows()
    data["growth_contribution"] = {
        "period_start": "2025-04",
        "period_end": "2026-04",
        "by_brand": {
            "top_contributors": [
                {
                    "brand": "리바로",
                    "is_target": True,
                    "contribution": -551_800_000,
                    "contribution_pct": -0.5518,
                }
            ]
        },
    }

    market = parse_general_market_response(
        deepcopy(payload),
        requested_atc4="C10A1",
        requested_source="ubist",
        requested_measure="sales",
        requested_brand="리바로",
    )
    resolution = ScopeResolution(
        scope=MarketScope(kind=MarketScopeKind.GENERAL_ATC4, atc4=("C10A1",)),
        source="ubist",
        normalized_arguments={"brand": "리바로"},
    )
    rendered = general_scope_result("market.get_market_size", market, resolution)["render_data"]

    assert rendered["market_cagr_5y_pct"] == 9.3677
    assert rendered["market_cagr_3y_pct"] == 7.1234
    assert rendered["brand_cagr_5y_pct"] == 3.9056
    assert rendered["brand_cagr_3y_pct"] == 2.3456
    assert rendered["analysis_levels"] == data["analysis_levels"]
    assert rendered["analysis_level_market_status"] == data["analysis_level_market_status"]
    assert rendered["level_top5_trend"] == data["level_top5_trend"]
    assert rendered["brand_trajectory"][0]["ei"] == 41.6922
    assert rendered["brand_trajectory"][0]["period_years"] == 5
    assert rendered["growth_contribution"]["growth_contribution_pct"] == -0.5518


def test_scope_view_set_renders_additive_dashboard_fields_from_evidence() -> None:
    render_data = {
        "brand": "리바로",
        "period": "2026-05",
        "market_cagr_5y_pct": 9.3677,
        "market_cagr_3y_pct": 7.1234,
        "brand_cagr_5y_pct": 3.9056,
        "brand_cagr_3y_pct": 2.3456,
        **_analysis_blocks(),
        "brand_trajectory": tuple(_trajectory_rows()["data"]),
        "growth_contribution": {
            "growth_contribution_pct": -0.5518,
            "period_start": "2025-05",
            "period_end": "2026-05",
        },
    }
    fact = MarketMetricFact(
        evidence_id="v3-shadow:market.get_brand_metric:adapterexpand",
        tool_name="market.get_brand_metric",
        arguments={"brand": "리바로"},
        raw_result={"render_data": render_data},
        missing_required_fields=(),
        entity="리바로",
        metric="sales",
        period="2026-05",
        unit="억원",
        view="general",
        market="C10A1",
    )
    bundle = V3EvidenceBundle(
        status="complete",
        facts=(fact,),
        failures=(),
        deferred=(),
        executions=(),
        original_call_count=1,
        executed_call_count=1,
        deduplicated_call_count=0,
    )

    result = build_scope_view_set(bundle, scope_confirmed=True)

    assert result.attached is True
    assert "CAGR" in result.view_names
    assert "Brand Trajectory" in result.view_names
    assert "전체 시장 analysis level" in result.view_names
    assert "고객 analysis level" in result.view_names
    assert "analysis-level Top5" in result.view_names
    assert "3.91" in result.markdown
    assert "41.7" in result.markdown
    assert "endpoint_5y" in result.markdown
    assert "Class 2" in result.markdown
    assert "시장 성장 기여도" in result.view_names
    assert result.charts == ()
