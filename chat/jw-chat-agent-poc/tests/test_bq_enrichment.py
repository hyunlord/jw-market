from __future__ import annotations

from jw_chat_agent_poc.orchestrator.answer_facts import answer_fact_markdown
from jw_chat_agent_poc.orchestrator.bq_enrichment import build_bq_analysis_call
from jw_chat_agent_poc.orchestrator.markdown_renderers import call_data_md


def test_c3_compares_source_values_without_aggregating_them() -> None:
    call = build_bq_analysis_call(
        "C3",
        [
            _market_call("ubist", "2026-05", [80.0, 81.0]),
            _market_call("iqvia_nsa", "2026-05", [84.0, 85.0]),
        ],
    )

    assert call is not None
    data = call["render_data"]
    assert data["calculation"] == "source_divergence"
    assert data["never_aggregate_sources"] is True
    assert data["absolute_delta_krw"] == 400_000_000.0
    assert "합산하지" in data["insights"][0]


def test_c3_does_not_compare_incompatible_periods() -> None:
    call = build_bq_analysis_call(
        "C3",
        [
            _market_call("ubist", "2026-05", [80.0, 81.0]),
            _market_call("iqvia_nsa", "2026-Q2", [84.0, 85.0]),
        ],
    )

    assert call is not None
    assert call["render_data"]["status"] == "incompatible_periods"
    assert "차이를 계산하지" in call["render_data"]["insights"][0]


def test_d2_aligns_only_common_periods_and_builds_dual_axis_chart() -> None:
    call = build_bq_analysis_call(
        "D2",
        [
            {
                "source": "CSD",
                "tool": "csd_activity_trend",
                "render_data": {
                    "series": [
                        {"period": "2026-01", "product_details": 12},
                        {"period": "2026-02", "product_details": None},
                        {"period": "2026-03", "product_details": 37},
                    ]
                },
            },
            _market_call("ubist", "2026-03", [80.0, None, 84.0]),
        ],
    )

    assert call is not None
    data = call["render_data"]
    assert data["calculation"] == "activity_performance_alignment"
    assert data["temporal_overlap_not_causation"] is True
    assert "시점이 겹칩니다" in data["insights"][0]
    chart = data["chart_payloads"][0]
    assert chart["chart_type"] == "dual_axis_line"
    assert chart["datasets"][0]["data"] == [12.0, None, 37.0]
    assert chart["datasets"][1]["data"] == [8_000_000_000.0, None, 8_400_000_000.0]


def test_d2_keeps_each_performance_source_separate() -> None:
    call = build_bq_analysis_call(
        "D2",
        [
            _activity_call(),
            _market_call("ubist", "2026-03", [80.0, 82.0, 84.0]),
            _market_call("iqvia_nsa", "2026-03", [90.0, 92.0, 95.0]),
        ],
    )

    assert call is not None
    data = call["render_data"]
    assert data["never_aggregate_sources"] is True
    assert [row["source"] for row in data["source_results"]] == ["UBIST", "IQVIA NSA"]
    assert {chart["source"] for chart in data["chart_payloads"]} == {
        "CSD+UBIST side-by-side",
        "CSD+IQVIA NSA side-by-side",
    }


def test_a2_forecast_is_explicitly_a_trend_extension_with_uncertainty() -> None:
    call = build_bq_analysis_call("A2", [_market_call("ubist", "2026-03", [100.0, 110.0, 121.0])])

    assert call is not None
    data = call["render_data"]
    assert data["calculation"] == "conditional_trend_forecast"
    assert data["forecast_krw"] == 13_310_000_000.0
    assert "추세 연장" in data["insights"][0]
    assert "반영하지" in data["insights"][0]


def test_a2_forecasts_each_market_source_separately() -> None:
    call = build_bq_analysis_call(
        "A2",
        [
            _market_call("ubist", "2026-03", [100.0, 110.0, 121.0]),
            _market_call("iqvia_nsa", "2026-03", [120.0, 126.0, 132.3]),
        ],
    )

    assert call is not None
    data = call["render_data"]
    assert data["never_aggregate_sources"] is True
    assert [row["source"] for row in data["source_results"]] == ["UBIST", "IQVIA NSA"]


def test_a3_preserves_patient_and_market_source_identity() -> None:
    call = build_bq_analysis_call(
        "A3",
        [
            {
                "source": "hira_disease",
                "tool": "get_disease_stats",
                "render_data": {
                    "calls": [
                        {
                            "render_data": {
                                "items": [{"ptntCnt": 1000, "year": "2026"}],
                            }
                        }
                    ]
                },
            },
            _market_call("ubist", "2026-05", [80.0]),
        ],
    )

    assert call is not None
    data = call["render_data"]
    assert data["calculation"] == "patient_sales_ratio"
    assert data["sales_per_patient_krw"] == 8_000_000.0
    assert data["source_labels"] == ["HIRA", "UBIST"]


def test_a3_computes_patient_ratio_per_market_source() -> None:
    call = build_bq_analysis_call(
        "A3",
        [
            _hira_call(),
            _market_call("ubist", "2026-05", [80.0]),
            _market_call("iqvia_nsa", "2026-05", [90.0]),
        ],
    )

    assert call is not None
    data = call["render_data"]
    assert data["never_aggregate_sources"] is True
    assert [row["source"] for row in data["source_results"]] == ["UBIST", "IQVIA NSA"]
    patient_rows = [row for row in data["evidence_ledger"] if row["source"] == "HIRA"]
    assert patient_rows == [
        {
            "source": "HIRA",
            "kind": "number",
            "identity": "get_disease_stats:items:ptntCnt:2026",
            "period": "2026",
            "value": 1000,
            "references": ["HIRA.render_data.items.ptntCnt"],
        }
    ]
    assert set(data["evidence_refs"]) == {
        "HIRA.render_data.items.ptntCnt",
        "UBIST.render_data.brand_value_series_10pt",
        "IQVIA NSA.render_data.brand_value_series_10pt",
    }


def test_a3_uses_sales_calls_once_when_share_calls_are_also_present() -> None:
    ubist_sales = _market_call("ubist", "2026-05", [80.0])
    iqvia_sales = _market_call("iqvia_nsa", "2026-05", [90.0])
    ubist_share = _market_call("ubist", "2026-05", [80.0])
    iqvia_share = _market_call("iqvia_nsa", "2026-05", [90.0])
    for call in (ubist_sales, iqvia_sales):
        call["render_data"]["query_spec"]["metrics"] = ["sales"]
    for call in (ubist_share, iqvia_share):
        call["render_data"]["metric"] = "market_share"
        call["render_data"]["query_spec"]["metrics"] = ["market_share"]

    call = build_bq_analysis_call(
        "A3",
        [_hira_call(), ubist_sales, iqvia_sales, ubist_share, iqvia_share],
    )

    assert call is not None
    assert [row["source"] for row in call["render_data"]["source_results"]] == [
        "UBIST",
        "IQVIA NSA",
    ]
    assert len(call["render_data"]["insights"]) == 2


def test_a3_fact_markdown_preserves_all_four_answer_elements() -> None:
    call = build_bq_analysis_call(
        "A3",
        [
            _hira_call(),
            _market_call("ubist", "2026-05", [80.0]),
            _market_call("iqvia_nsa", "2026-05", [90.0]),
        ],
    )

    assert call is not None
    fact_md = answer_fact_markdown([call], [])
    assert "HIRA 환자수" in fact_md
    assert "UBIST 매출·환자당 관측비" in fact_md
    assert "IQVIA NSA 매출·환자당 관측비" in fact_md
    assert "기간·정의 정렬" in fact_md
    assert "합산하지" in fact_md
    assert "### 환자수·매출 병렬 비교 fact" in fact_md
    assert "데이터 없음" not in fact_md

    data_md = call_data_md(call)
    assert "### 환자수·매출 병렬 비교" in data_md
    assert "UBIST" in data_md
    assert "IQVIA NSA" in data_md
    assert "합산하지 않음" in data_md
    assert "데이터 없음" not in data_md


def test_d3_reports_missing_seller_axis_instead_of_substituting_brand_activity() -> None:
    call = build_bq_analysis_call(
        "D3",
        [{"source": "CSD", "tool": "csd_activity_trend", "render_data": {"series": []}}],
    )

    assert call is not None
    assert call["render_data"]["status"] == "unsupported_axis"
    assert "판매사별" in call["render_data"]["insights"][0]


def test_d3_calculates_competitor_seller_share_change_without_target_company() -> None:
    call = build_bq_analysis_call(
        "D3",
        [
            {
                "source": "CSD",
                "tool": "csd_activity_trend",
                "render_data": {
                    "anchor_companies": ["JW PHARM"],
                    "seller_series": [
                        {"period": "2026-01", "company": "JW PHARM", "product_details": 12},
                        {"period": "2026-01", "company": "COMPETITOR", "product_details": 18},
                        {"period": "2026-03", "company": "JW PHARM", "product_details": 37},
                        {"period": "2026-03", "company": "COMPETITOR", "product_details": 63},
                    ],
                },
            }
        ],
    )

    assert call is not None
    data = call["render_data"]
    assert data["status"] == "ok"
    assert data["seller_results"][0]["company"] == "COMPETITOR"
    assert data["seller_results"][0]["start_share_pct"] == 60.0
    assert data["seller_results"][0]["latest_share_pct"] == 63.0
    assert data["seller_results"][0]["share_delta_pctp"] == 3.0
    assert "JW PHARM" not in " ".join(data["insights"])
    assert any(
        "CSD.render_data.seller_series" in reference
        for row in data["evidence_ledger"]
        for reference in row.get("references", [])
    )


def _market_call(source: str, period: str, values_eok: list[float | None]) -> dict[str, object]:
    periods = [f"2026-{index + 1:02d}" for index in range(len(values_eok))]
    return {
        "source": "IQVIA" if source == "iqvia_nsa" else "UBIST",
        "tool": "get_brand_metric",
        "render_data": {
            "brand": "리바로",
            "metric": "series",
            "period": period,
            "sales_krw": None if not values_eok else _krw(values_eok[-1]),
            "brand_value_series_10pt": [
                {"period": item_period, "value_krw": _krw(value)}
                for item_period, value in zip(periods, values_eok, strict=True)
            ],
            "query_spec": {"source": source},
        },
    }


def _activity_call() -> dict[str, object]:
    return {
        "source": "CSD",
        "tool": "csd_activity_trend",
        "render_data": {
            "series": [
                {"period": "2026-01", "product_details": 12},
                {"period": "2026-02", "product_details": 20},
                {"period": "2026-03", "product_details": 37},
            ]
        },
    }


def _hira_call() -> dict[str, object]:
    return {
        "source": "hira_disease",
        "tool": "get_disease_stats",
        "render_data": {
            "calls": [{"render_data": {"items": [{"ptntCnt": 1000, "year": "2026"}]}}]
        },
    }


def _krw(value: float | None) -> float | None:
    return None if value is None else value * 100_000_000
