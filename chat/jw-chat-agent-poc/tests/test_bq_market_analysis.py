from __future__ import annotations

import pytest

from jw_chat_agent_poc.orchestrator.bq_enrichment import build_bq_analysis_call
from jw_chat_agent_poc.orchestrator.bq_runtime_guard import validate_bq_analysis_call


def test_a1_reports_each_source_separately_with_long_window_growth() -> None:
    call = build_bq_analysis_call(
        "A1",
        [_series_call("ubist"), _series_call("iqvia_nsa", scale=1.2), _dimension_call("channel")],
    )

    assert call is not None
    data = call["render_data"]
    assert data["never_aggregate_sources"] is True
    assert [item["source"] for item in data["source_summaries"]] == ["UBIST", "IQVIA NSA"]
    assert data["source_summaries"][0]["growth_rate_pct"] == pytest.approx(20.0)
    assert data["channel_shares_pct"] == {"의원": 70.0, "종병": 30.0}
    assert data["fusion_mode"] == "side_by_side"
    assert all(
        row.get("source") and row.get("kind") and row.get("identity")
        for row in data["evidence_ledger"]
    )


def test_a1_zero_baseline_is_reported_without_crashing_or_minus_hundred() -> None:
    series = _series_call("ubist")
    series["render_data"]["brand_value_series_10pt"][0]["value_krw"] = 0

    call = build_bq_analysis_call("A1", [series])

    assert call is not None
    data = call["render_data"]
    assert data["source_summaries"][0]["growth_rate_pct"] is None
    assert "기준값이 0" in data["insights"][0]
    assert "-100" not in data["insights"][0]


def test_b1_computes_share_of_growth_decomposition_and_gain_loss() -> None:
    call = build_bq_analysis_call("B1", [_series_call("ubist"), _top_call()])

    assert call is not None
    data = call["render_data"]
    assert data["share_of_growth_pct"] == pytest.approx(20.0)
    assert data["brand_growth_pct"] == pytest.approx(20.0)
    assert data["market_growth_pct"] == pytest.approx(10.0)
    assert data["excess_growth_pctp"] == pytest.approx(10.0)
    assert data["share_delta_pctp"] == pytest.approx(0.91)
    assert data["gain_loss"][0]["brand"] == "리바로"
    assert data["chart_payloads"][0]["chart_type"] == "waterfall"


def test_b1_ledger_binds_waterfall_to_top5_trend_rows() -> None:
    call = build_bq_analysis_call("B1", [_series_call("ubist"), _top_call()])

    assert call is not None
    ledger = call["render_data"]["evidence_ledger"]
    trend_rows = [row for row in ledger if row["kind"] == "trend"]
    assert {row["identity"] for row in trend_rows} == {
        "get_brand_metric:level_top5_trend_series:리바로",
        "get_brand_metric:level_top5_trend_series:로수젯",
        "get_brand_metric:level_top5_trend_series:경쟁A",
    }
    assert all("UBIST.level_top5_trend_series" in row["references"] for row in trend_rows)
    validate_bq_analysis_call(call)


def test_b1_keeps_each_source_calculation_separate() -> None:
    call = build_bq_analysis_call(
        "B1",
        [
            _series_call("ubist"),
            _series_call("iqvia_nsa", scale=1.2),
            _top_call("ubist"),
            _top_call("iqvia_nsa", scale=1.2),
        ],
    )

    assert call is not None
    data = call["render_data"]
    assert data["never_aggregate_sources"] is True
    assert [row["source"] for row in data["source_results"]] == ["UBIST", "IQVIA NSA"]
    assert {chart["source"] for chart in data["chart_payloads"]} == {"UBIST", "IQVIA NSA"}


def test_b2_computes_cohort_position_from_same_market_population() -> None:
    call = build_bq_analysis_call("B2", [_series_call("ubist"), _top_call()])

    assert call is not None
    data = call["render_data"]
    assert data["population"] == 3
    assert data["cohort_z_score"] == pytest.approx(-0.267261, abs=0.000001)
    assert data["competition_basis"] == "same market source and period"


def test_b3_does_not_invent_launch_acceleration_without_launch_date() -> None:
    call = build_bq_analysis_call("B3", [_series_call("ubist"), _top_call()])

    assert call is not None
    data = call["render_data"]
    assert data["launch_acceleration_status"] == "unsupported_missing_launch_date"
    assert data["growth_ranking"][0]["brand"] == "리바로"


def test_c1_compares_brand_growth_with_market_growth() -> None:
    call = build_bq_analysis_call("C1", [_series_call("ubist")])

    assert call is not None
    data = call["render_data"]
    assert data["growth_gap_pctp"] == pytest.approx(10.0)
    assert data["trend_slope_krw_per_period"] == pytest.approx(2_000_000_000.0)


def test_c1_keeps_each_source_growth_gap_separate() -> None:
    call = build_bq_analysis_call(
        "C1", [_series_call("ubist"), _series_call("iqvia_nsa", scale=1.2)]
    )

    assert call is not None
    data = call["render_data"]
    assert data["never_aggregate_sources"] is True
    assert [row["source"] for row in data["source_results"]] == ["UBIST", "IQVIA NSA"]


def test_c2_normalizes_channel_and_specialty_without_cross_axis_sum() -> None:
    call = build_bq_analysis_call("C2", [_dimension_call("channel"), _dimension_call("specialty")])

    assert call is not None
    data = call["render_data"]
    assert data["distributions"]["channel"] == {"의원": 70.0, "종병": 30.0}
    assert data["distributions"]["specialty"] == {"순환기": 60.0, "내분비": 40.0}
    assert data["axes_are_not_aggregated"] is True


def test_c2_ledger_keeps_channel_and_specialty_rows_distinct() -> None:
    call = build_bq_analysis_call("C2", [_dimension_call("channel"), _dimension_call("specialty")])

    assert call is not None
    ledger = call["render_data"]["evidence_ledger"]
    assert {row["identity"] for row in ledger} == {
        "query:channel:level_segments:의원",
        "query:channel:level_segments:종병",
        "query:specialty:level_segments:순환기",
        "query:specialty:level_segments:내분비",
    }
    assert {reference for row in ledger for reference in row["references"]} >= {
        "UBIST.channel.level_segments", "UBIST.specialty.level_segments"
    }
    validate_bq_analysis_call(call)


def test_a3_uses_hira_request_year_for_patient_evidence() -> None:
    market = _series_call("ubist")
    market["render_data"]["sales_krw"] = 12_000_000_000
    call = build_bq_analysis_call(
        "A3",
        [
            market,
            {
                "tool": "get_disease_stats",
                "source": "hira_disease",
                "render_data": {
                    "calls": [
                        {
                            "render_data": {
                                "request": {"sickCd": "E78", "year": "2024"},
                                "items": [{"inpatOpat": "외래", "ptntCnt": 1_305_727}],
                            }
                        }
                    ]
                },
            },
        ],
    )

    assert call is not None
    assert call["render_data"]["patient_period"] == "2024"
    assert any(
        row.get("source") == "HIRA" and row.get("period") == "2024"
        for row in call["render_data"]["evidence_ledger"]
    )
    validate_bq_analysis_call(call)


def test_d1_reports_activity_change_and_honest_topic_gap() -> None:
    call = build_bq_analysis_call(
        "D1",
        [{"tool": "csd_activity_trend", "render_data": {"series": [{"period": "2026-01", "product_details": 12}, {"period": "2026-03", "product_details": 37}]}}],
    )

    assert call is not None
    data = call["render_data"]
    assert data["activity_delta"] == 25.0
    assert data["topic_status"] == "unsupported_by_current_csd_tool"
    assert data["region"] == "TOTAL"
    assert data["market2_excluded"] is True


def test_e1_keeps_only_news_with_complete_identity() -> None:
    call = build_bq_analysis_call(
        "E1",
        [
            {
                "tool": "deep_analysis_related_news",
                "render_data": {
                    "items": [
                        {"title": "리바로 기사", "date": "2026-05-01", "source": "약업신문", "url": "https://news.example/1"},
                        {"title": "URL 없는 기사", "date": "2026-05-02", "source": "약업신문", "url": ""},
                    ]
                },
            }
        ],
    )

    assert call is not None
    assert call["render_data"]["news_refs"] == [
        {"title": "리바로 기사", "date": "2026-05-01", "source": "약업신문", "url": "https://news.example/1"}
    ]


def test_e2_fuses_grounded_sources_without_aggregation_or_causal_overclaim() -> None:
    call = build_bq_analysis_call(
        "E2",
        [
            _series_call("ubist"),
            _series_call("iqvia_nsa", scale=1.2),
            _top_call(),
            {
                "tool": "csd_activity_trend",
                "render_data": {
                    "series": [
                        {"period": "2025-01", "product_details": 12},
                        {"period": "2026-01", "product_details": 37},
                    ]
                },
            },
            {
                "tool": "get_disease_stats",
                "render_data": {
                    "calls": [{"render_data": {"items": [{"ptntCnt": 1000, "year": "2026"}]}}]
                },
            },
            {
                "tool": "deep_analysis_related_news",
                "render_data": {
                    "items": [
                        {
                            "title": "리바로 시장 기사",
                            "date": "2026-01-15",
                            "source": "약업신문",
                            "url": "https://news.example/e2",
                        },
                        {"title": "근거 불완전", "date": "2026-01-16", "source": "약업신문", "url": ""},
                    ]
                },
            },
        ],
    )

    assert call is not None
    data = call["render_data"]
    assert data["calculation"] == "cross_source_causal_context"
    assert data["never_aggregate_sources"] is True
    assert data["causal_posture"] == "temporal_overlap_not_causation"
    assert data["news_refs"] == [
        {
            "title": "리바로 시장 기사",
            "date": "2026-01-15",
            "source": "약업신문",
            "url": "https://news.example/e2",
        }
    ]
    assert "시점이 겹칩니다" in " ".join(data["insights"])
    assert "인과를 단정하지" in " ".join(data["insights"])
    assert {item["source"] for item in data["evidence_ledger"]} >= {
        "UBIST",
        "IQVIA NSA",
        "CSD",
        "HIRA",
        "NEWS",
    }


def _series_call(source: str, scale: float = 1.0) -> dict[str, object]:
    return {
        "tool": "get_brand_metric",
        "render_data": {
            "brand": "리바로",
            "period": "2026-01",
            "query_spec": {"source": source},
            "brand_value_series_10pt": [
                {"period": "2025-01", "value_krw": 10_000_000_000 * scale, "ms_pct": 10.0, "rank": 2},
                {"period": "2026-01", "value_krw": 12_000_000_000 * scale, "ms_pct": 10.91, "rank": 2},
            ],
            "market_size_series": [
                {"period": "2025-01", "value_krw": 100_000_000_000 * scale},
                {"period": "2026-01", "value_krw": 110_000_000_000 * scale},
            ],
        },
    }


def _top_call(source: str = "ubist", scale: float = 1.0) -> dict[str, object]:
    return {
        "tool": "get_brand_metric",
        "render_data": {
            "metric": "market_top_brands",
            "query_spec": {"source": source},
            "level_segments": [
                {"name": "리바로", "value": 12_000_000_000 * scale, "rank": 2},
                {"name": "로수젯", "value": 20_000_000_000 * scale, "rank": 1},
                {"name": "경쟁A", "value": 8_000_000_000 * scale, "rank": 3},
            ],
            "level_top5_trend_series": [
                {"brand": "리바로", "from_ms_pct": 10.0, "to_ms_pct": 10.91, "share_delta_pctp": 0.91, "value_delta_krw": 2_000_000_000 * scale},
                {"brand": "로수젯", "from_ms_pct": 20.0, "to_ms_pct": 19.0, "share_delta_pctp": -1.0, "value_delta_krw": 1_000_000_000 * scale},
                {"brand": "경쟁A", "from_ms_pct": 8.0, "to_ms_pct": 7.0, "share_delta_pctp": -1.0, "value_delta_krw": -500_000_000 * scale},
            ],
        },
    }


def _dimension_call(dimension: str) -> dict[str, object]:
    rows = (
        [{"name": "의원", "value": 70}, {"name": "종병", "value": 30}]
        if dimension == "channel"
        else [{"name": "순환기", "value": 60}, {"name": "내분비", "value": 40}]
    )
    return {
        "tool": "query",
        "render_data": {"requested_dimension": dimension, "level_segments": rows, "query_spec": {"source": "ubist"}},
    }
