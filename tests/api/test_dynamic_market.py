from __future__ import annotations

import json
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.scripts.api.dynamic_market.aggregator import MetricAggregator, compute_cagr, compute_hhi
from pipeline.scripts.api.dynamic_market.aggregator import sidecar_rows_to_metric_rows
from pipeline.scripts.api.dynamic_market.composer import ResponseComposer
from pipeline.scripts.api.dynamic_market.resolvers import GeneralViewResolver
from pipeline.scripts.api.dynamic_market.types import (
    AggregatedMetrics,
    BrandMetric,
    BrandRef,
    DimensionFilter,
    DynamicMarketInputError,
    MarketDefinition,
    PeriodRange,
)


def test_compute_hhi_when_brand_shares_are_known() -> None:
    brands = (
        BrandMetric("a", "A", "C10B", "demo", 75.0, 75.0, 1, "2026-04", 75.0),
        BrandMetric("b", "B", "C10B", "demo", 25.0, 25.0, 2, "2026-04", 25.0),
    )

    assert compute_hhi(brands) == 6250.0


def test_compute_cagr_accepts_iqvia_quarter_periods() -> None:
    series = (
        {"period": "2024-Q1", "market_size": 100.0},
        {"period": "2025-Q1", "market_size": 121.0},
    )

    assert compute_cagr(series) == 21.0


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

    assert response["market_meta"]["view_source_id"] == "dynamic_general"
    assert response["data"]["market_size_series"][0]["value"] == 100.0


def test_reject_disabled_analysis_level_when_molecule_is_requested() -> None:
    resolver = GeneralViewResolver(mart_db="jw_mart", bridge_db="jw_mart")

    try:
        resolver.resolve(
            atc4=["A10A1"],
            molecule=[],
            analysis_level={"ubist": {"molecule": ["PITAVASTATIN"]}},
            focus_brand_key=None,
            source="ubist",
            measure="sales",
        )
    except DynamicMarketInputError as exc:
        assert "disabled" in str(exc)
    else:
        raise AssertionError("disabled molecule dimension was accepted")


def test_default_focus_brand_requires_one_unambiguous_atc4(monkeypatch) -> None:
    resolver = GeneralViewResolver(mart_db="jw_mart", bridge_db="jw_mart")

    def fake_fetch_all(sql: str, params: tuple[str, ...]) -> list[dict]:
        if "SELECT DISTINCT atc4_code" in sql:
            return [{"atc4_code": "A10A1"}]
        return [{"brand_key": "brand-a", "brand_name": "Brand A", "atc4_code": "A10A1"}]

    monkeypatch.setattr("pipeline.scripts.api.dynamic_market.resolvers.db.fetch_all", fake_fetch_all)
    definition = resolver.resolve(
        atc4=[],
        molecule=[],
        analysis_level=None,
        focus_brand_key="brand-a",
        source="ubist",
        measure="sales",
    )

    assert definition.filter_echo["atc4"] == ["A10A1"]
    assert definition.focus_brand_key == "brand-a"


def test_sidecar_rows_require_all_dimensions_without_product_overinclude() -> None:
    rows = [
        {
            "brand_key": "brand-a",
            "brand_name": "Brand A",
            "atc4_code": "A10A1",
            "product_code": "p1",
            "dimension_type": "form",
            "raw_value_history": json.dumps({"2026-01": 10}),
        },
        {
            "brand_key": "brand-a",
            "brand_name": "Brand A",
            "atc4_code": "A10A1",
            "product_code": "p1",
            "dimension_type": "route",
            "raw_value_history": json.dumps({"2026-01": 10}),
        },
        {
            "brand_key": "brand-a",
            "brand_name": "Brand A",
            "atc4_code": "A10A1",
            "product_code": "p2",
            "dimension_type": "form",
            "raw_value_history": json.dumps({"2026-01": 90}),
        },
    ]

    metric_rows = sidecar_rows_to_metric_rows(
        rows,
        metadata={("brand-a", "A10A1"): {"atc4_desc": "demo", "unit_label": "KRW"}},
        required_dimensions=("form", "route"),
    )

    assert len(metric_rows) == 1
    assert json.loads(metric_rows[0]["raw_value_history"]) == {"2026-01": 10.0}


def test_dimension_filter_predicate_uses_sidecar_product_rows(monkeypatch) -> None:
    calls: list[str] = []

    def fake_fetch_all(sql: str, params: tuple[str, ...]) -> list[dict]:
        calls.append(sql)
        if "mart_general_filter_dimension_metric" in sql:
            return [
                {
                    "brand_key": "brand-a",
                    "brand_name": "Brand A",
                    "atc4_code": "A10A1",
                    "product_code": "p1",
                    "dimension_type": "form",
                    "raw_value_history": json.dumps({"2026-01": 10}),
                }
            ]
        return [{"brand_key": "brand-a", "atc4_code": "A10A1", "atc4_desc": "demo", "unit_label": "KRW"}]

    monkeypatch.setattr("pipeline.scripts.api.dynamic_market.aggregator.db.fetch_all", fake_fetch_all)
    metrics = MetricAggregator(mart_db="jw_mart").aggregate(
        brands=(BrandRef("brand-a", "Brand A", "A10A1"),),
        source="ubist",
        measure="sales",
        period_range=PeriodRange(),
        top_n=20,
        dimension_filters=(DimensionFilter("form", ("정제",)),),
    )

    assert metrics.market_size == 10.0
    assert any("mart_general_filter_dimension_metric" in sql for sql in calls)
