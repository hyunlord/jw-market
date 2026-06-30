from __future__ import annotations

import json
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.scripts.api.dynamic_market.aggregator import MetricAggregator, compute_hhi
from pipeline.scripts.api.dynamic_market.composer import ResponseComposer
from pipeline.scripts.api.dynamic_market import strategic_runtime
from pipeline.scripts.api.dynamic_market.types import AggregatedMetrics, BrandMetric, MarketDefinition, PeriodRange
from pipeline.scripts.api.models.dynamic_market import DynamicMarketRequest
from pipeline.scripts.api.routes import dynamic_market as dynamic_market_route


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


def test_request_accepts_strategic_frontend_filters() -> None:
    request = DynamicMarketRequest.model_validate(
        {
            "source": "ubist",
            "measure": "sales",
            "filters": {
                "view_kind": "market_landscape",
                "ml_id": "ml_006",
                "focus_brand_key": "리바로젯",
                "analysis_level": {
                    "ubist": {
                        "seller": ["JW중외제약"],
                        "atc4": ["C10C"],
                    },
                    "iqvia": {},
                },
            },
        }
    )

    assert request.filters.ml_id == "ml_006"
    assert request.filters.focus_brand_key == "리바로젯"
    assert request.filters.analysis_level.ubist.seller == ["JW중외제약"]


def test_route_returns_envelope_for_general_dynamic_market(monkeypatch) -> None:
    class FakeResolver:
        def __init__(self, *, mart_db: str, bridge_db: str) -> None:
            assert mart_db
            assert bridge_db

        def resolve(self, *, atc4, molecule, source, measure):
            return MarketDefinition(
                view="general",
                filter_echo={
                    "view": "general",
                    "atc4": list(atc4),
                    "molecule": list(molecule),
                    "source": source,
                    "measure": measure,
                },
                source=source,
                measure=measure,
                brands=(),
            )

    class FakeAggregator:
        def __init__(self, *, mart_db: str) -> None:
            assert mart_db

        def aggregate(self, *, brands, source, measure, period_range, top_n):
            return AggregatedMetrics(
                source=source,
                measure=measure,
                unit_label="KRW",
                market_size=1.0,
                hhi=None,
                cagr=None,
                monthly_series=({"period": "2026-04", "market_size": 1.0},),
                brands=(),
            )

    monkeypatch.setattr(dynamic_market_route, "GeneralViewResolver", FakeResolver)
    monkeypatch.setattr(dynamic_market_route, "MetricAggregator", FakeAggregator)

    response = dynamic_market_route.dynamic_market(
        DynamicMarketRequest.model_validate({"filters": {"atc4": ["C10C"]}, "source": "ubist", "measure": "sales"})
    )

    assert response["status"] == "SUCCESS"
    assert response["result"]["market_meta"]["market_size_recent"] == 1.0


def test_route_uses_strategic_runtime_for_ml_id(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_build_strategic_payload(**kwargs):
        captured.update(kwargs)
        return {
            "brand": "리바로젯",
            "source": "UBIST",
            "data": {"analysis_levels": {"levels": ["Class"]}},
            "market_meta": {"market_size_recent": 226.0},
        }

    monkeypatch.setattr(dynamic_market_route, "build_strategic_payload", fake_build_strategic_payload)

    response = dynamic_market_route.dynamic_market(
        DynamicMarketRequest.model_validate(
            {
                "source": "ubist",
                "measure": "sales",
                "filters": {"ml_id": "ml_006", "focus_brand_key": "리바로젯"},
            }
        )
    )

    assert response["status"] == "SUCCESS"
    assert response["result"]["data"]["analysis_levels"]["levels"] == ["Class"]
    assert captured["ml_id"] == "ml_006"
    assert captured["focus_brand_key"] == "리바로젯"


def test_strategic_runtime_reuses_cache_cause_builder(monkeypatch) -> None:
    market_row = {
        "id": 1,
        "ml_id": "ml_006",
        "source": "ubist",
        "measure": "sales",
        "market_size_series": json.dumps({"2026-04": 300.0}),
        "hhi_series_5y": json.dumps({}),
        "brand_ranking_stacked": json.dumps({}),
        "company_ranking_stacked": json.dumps({}),
    }
    brand_rows = [
        {
            "id": 10,
            "ml_id": "ml_006",
            "brand_key": "리바로젯",
            "brand_name": "리바로젯",
            "company_name": "JW중외제약",
            "source": "ubist",
            "measure": "sales",
            "unit_label": "KRW",
            "is_jw": 1,
            "metric_history": json.dumps({"2026-04": {"raw_value": 100.0, "ms": 33.3333, "rank": 3}}),
            "extended_metric_history": json.dumps({}),
            "raw_value_history": json.dumps({"2026-04": 100.0}),
            "by_dimension": json.dumps({"seller": "JW중외제약", "atc4_code": "C10C"}),
        },
        {
            "id": 11,
            "ml_id": "ml_006",
            "brand_key": "경쟁",
            "brand_name": "경쟁",
            "company_name": "경쟁사",
            "source": "ubist",
            "measure": "sales",
            "unit_label": "KRW",
            "is_jw": 0,
            "metric_history": json.dumps({"2026-04": {"raw_value": 200.0, "ms": 66.6667, "rank": 1}}),
            "extended_metric_history": json.dumps({}),
            "raw_value_history": json.dumps({"2026-04": 200.0}),
            "by_dimension": json.dumps({"seller": "경쟁사", "atc4_code": "C10C"}),
        },
    ]
    captured: dict[str, object] = {}

    def fake_fetch_all(sql, params):
        assert "mart_strategic_ml_brand_metric" in sql
        assert params == ["ml_006", "ubist", "sales"]
        return brand_rows

    def fake_fetch_one(sql, params):
        assert "mart_strategic_ml_market_metric" in sql
        assert params == ["ml_006", "ubist", "sales"]
        return market_row

    def fake_build_response(**kwargs):
        captured.update(kwargs)
        return {
            "brand": kwargs["brand_row"]["brand_name"],
            "source": kwargs["source"],
            "measure": kwargs["measure"],
            "data": {
                "kpi": {"target_rank": 3},
                "analysis_levels": {"levels": ["Class"]},
                "level_top5_trend": {"by_level": {"Class": []}},
                "target_customer_competition": {"targets": []},
                "ubist_specialty_channels": ["종합병원"],
            },
            "market_meta": {"market_size_recent": 300.0},
        }

    monkeypatch.setattr(strategic_runtime.db, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(strategic_runtime.db, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(
        strategic_runtime,
        "_catalog_row",
        lambda market_kind, view_source_id: {"ml_id": view_source_id, "name": "리바로 시장"},
    )
    monkeypatch.setattr(strategic_runtime, "_strategic_brand_catalog", lambda: None)
    monkeypatch.setattr(strategic_runtime.cause_builder, "build_response", fake_build_response)

    result = strategic_runtime.build_strategic_payload(
        mart_db="jw_mart",
        ml_id="ml_006",
        cd_market_id=None,
        focus_brand_key="리바로젯",
        source="ubist",
        measure="sales",
        analysis_level=DynamicMarketRequest().filters.analysis_level,
    )

    assert result["data"]["analysis_levels"]["levels"] == ["Class"]
    assert result["data"]["kpi"]["target_rank"] == 3
    assert captured["brand_row"]["brand_name"] == "리바로젯"
    assert captured["market_id"] == "strategy_006"
    assert captured["source"] == "UBIST"


def test_runtime_channel_resolver_falls_back_to_mart_specialty_data() -> None:
    rows = [
        {
            "brand_name": "리바로젯",
            "specialty_data": json.dumps(
                {
                    "종합병원 순환기": {"2026-04": {"raw_value": 50.0}},
                    "의원 IGF": {"2026-04": {"raw_value": 20.0}},
                    "Others(병원,보건기관, 그 외 요양기관)": {"2026-04": {"raw_value": 999.0}},
                }
            ),
        }
    ]

    def empty_original_resolver(*, rows, market, measure, max_channels):
        return {
            "channels": ["전체", "상급종병", "종병", "병원", "의원", "보건소", "기타"],
            "specialty_channels": ["전체"],
        }

    resolver = strategic_runtime._runtime_resolve_market_channels(empty_original_resolver)
    result = resolver(rows=rows, market={}, measure="sales", max_channels=4)

    assert result["specialty_channels"] == ["전체", "종합병원 순환기", "의원 IGF"]
    assert rows[0]["__ubist_specialty_channel_data"]["종합병원 순환기"]["2026-04"]["raw_value"] == 50.0
