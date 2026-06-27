from __future__ import annotations

import json
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.scripts.api.dynamic_market.aggregator import MetricAggregator, compute_hhi
from pipeline.scripts.api.dynamic_market.composer import ResponseComposer
from pipeline.scripts.api.dynamic_market.types import AggregatedMetrics, BrandMetric, MarketDefinition, PeriodRange


def test_compute_hhi_when_brand_shares_are_known() -> None:
    brands = (
        BrandMetric("a", "A", "C10B", "", 75.0, 75.0, 1, "2026-04", 75.0),
        BrandMetric("b", "B", "C10B", "", 25.0, 25.0, 2, "2026-04", 25.0),
    )

    assert compute_hhi(brands) == 6250.0


def test_aggregate_rows_when_period_range_limits_history() -> None:
    aggregator = MetricAggregator(mart_db="jw_mart")
    rows = [
        {
            "brand_key": "a",
            "brand_name": "A",
            "atc4_code": "C10B",
            "unit_label": "KRW",
            "raw_value_history": json.dumps({"2026-01": 10, "2026-02": 20, "2026-03": 40}),
        },
        {
            "brand_key": "b",
            "brand_name": "B",
            "atc4_code": "C10B",
            "unit_label": "KRW",
            "raw_value_history": json.dumps({"2026-01": 30, "2026-02": 20, "2026-03": 10}),
        },
    ]

    brand_metrics, monthly_totals = aggregator._aggregate_rows(rows, period_range=PeriodRange("2026-02", "2026-03"))

    assert monthly_totals == {"2026-02": 40.0, "2026-03": 50.0}
    assert [item.total_value for item in brand_metrics] == [60.0, 30.0]


def test_compose_when_definition_and_metrics_are_ready() -> None:
    definition = MarketDefinition(
        view="general",
        filter_echo={"view": "general", "atc4": ["C10B"], "molecule": [], "source": "ubist", "measure": "sales"},
        source="ubist",
        measure="sales",
    )
    metrics = AggregatedMetrics(
        source="ubist",
        measure="sales",
        unit_label="KRW",
        market_size=100.0,
        hhi=5000.0,
        cagr=None,
        monthly_series=({"period": "2026-04", "market_size": 100.0},),
        brands=(),
    )

    response = ResponseComposer().compose(definition=definition, metrics=metrics)

    assert response["market_meta"]["market_size_recent"] == 100.0
    assert response["data"]["market_size_series"][0]["value"] == 100.0


def test_compose_emits_only_portal_read_cause_sections() -> None:
    definition = MarketDefinition(
        view="general",
        filter_echo={"view": "general", "atc4": ["C10B"], "molecule": [], "source": "ubist", "measure": "sales"},
        source="ubist",
        measure="sales",
    )
    metrics = AggregatedMetrics(
        source="ubist",
        measure="sales",
        unit_label="KRW",
        market_size=300.0,
        hhi=5555.0,
        cagr=10.0,
        monthly_series=(
            {"period": "2025-01", "market_size": 100.0},
            {"period": "2026-01", "market_size": 200.0},
        ),
        brands=(),
        all_brands=(
            BrandMetric(
                "focus",
                "포커스",
                "C10B",
                "",
                200.0,
                66.666667,
                1,
                "2026-01",
                150.0,
                ({"period": "2025-01", "value": 50.0}, {"period": "2026-01", "value": 150.0}),
            ),
            BrandMetric(
                "other",
                "경쟁",
                "C10B",
                "",
                100.0,
                33.333333,
                2,
                "2026-01",
                50.0,
                ({"period": "2025-01", "value": 50.0}, {"period": "2026-01", "value": 50.0}),
            ),
        ),
    )

    response = ResponseComposer().compose(definition=definition, metrics=metrics)
    data = response["data"]

    assert "data_period_coverage" not in data
    assert "resolved_scope" not in response
    assert {"market_size_recent", "target_share_pct", "target_rank", "market_cagr_5y_pct", "direct_competition_count"}.issubset(
        data["kpi"]
    )
    assert {"market_size_series", "hhi_series_5y"}.issubset(data["sources_data"])
    assert {"period", "value"}.issubset(data["sources_data"]["market_size_series"][0])
    latest_brand_row = data["brand_ranking_stacked"]["yearly"][-1]["rankings"][0]
    assert {"brand", "ms_pct", "rank", "is_target", "is_others", "value"}.issubset(latest_brand_row)
    latest_company_row = data["company_ranking_stacked"]["yearly"][-1]["rankings"][0]
    assert {"company", "ms_pct", "rank", "is_others"}.issubset(latest_company_row)
    assert {"levels", "channels", "period_unit", "periods_monthly", "periods_quarterly", "data"}.issubset(
        data["analysis_levels"]
    )
    assert {"available_levels", "default_level", "by_level"}.issubset(data["level_top5_trend"])
    assert isinstance(data["ei_ms_matrix"]["data"], list)
    assert isinstance(data["growth_contribution_ms_matrix"]["data"], list)
    assert "targets" in data["target_customer_competition"]
