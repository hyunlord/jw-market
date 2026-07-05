from __future__ import annotations

import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.scripts.api.dynamic_market import cause_payload, resolvers
from pipeline.scripts.api.dynamic_market.aggregator import MetricAggregator, compute_cagr, compute_hhi
from pipeline.scripts.api.dynamic_market.aggregator import sidecar_rows_to_metric_rows
from pipeline.scripts.api.dynamic_market.composer import ResponseComposer
from pipeline.scripts.api.dynamic_market.cause_payload import build_cause_payload
from pipeline.scripts.api.dynamic_market.resolvers import GeneralViewResolver, StrategicViewResolver
from pipeline.scripts.api.dynamic_market.types import (
    AggregatedMetrics,
    BrandMetric,
    BrandRef,
    DimensionFilter,
    DynamicMarketInputError,
    MarketDefinition,
    PeriodRange,
)
from pipeline.scripts.api.models.dynamic_market import DynamicMarketRequest
from pipeline.scripts.api.routes import dynamic_market as dynamic_market_route


def test_compute_hhi_when_brand_shares_are_known() -> None:
    brands = (
        BrandMetric("a", "A", "C10B", 75.0, 75.0, 1, "2026-04", 75.0),
        BrandMetric("b", "B", "C10B", 25.0, 25.0, 2, "2026-04", 25.0),
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


def test_general_aggregate_reads_raw_matrix_without_derived_channel_columns(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_fetch_all(sql: str, params: tuple[object, ...]) -> list[dict[str, object]]:
        captured["sql"] = sql
        captured["params"] = params
        assert "ubist_channel_by_display" not in sql
        assert "ubist_channel_by_code" not in sql
        return [
            {
                "brand_key": "a",
                "brand_name": "A",
                "atc4_code": "C10A1",
                "source": "ubist",
                "measure": "sales",
                "unit_label": "KRW",
                "raw_value_history": json.dumps({"2026-05": 100.0}),
                "channel_specialty_matrix": json.dumps(
                    {"종합병원": {"순환기(Cardiology IM)": {"2026-05": 90.0}}},
                    ensure_ascii=False,
                ),
            }
        ]

    monkeypatch.setattr("pipeline.scripts.api.dynamic_market.aggregator.db.fetch_all", fake_fetch_all)

    metrics = MetricAggregator(mart_db="jw_mart").aggregate(
        brands=(BrandRef("a", "A", "C10A1"),),
        source="ubist",
        measure="sales",
        period_range=PeriodRange(),
        top_n=20,
    )

    assert "channel_specialty_matrix" in str(captured["sql"])
    assert captured["params"] == ("ubist", "sales", "a", "C10A1")
    assert metrics.all_brands[0].channel_specialty_matrix == {
        "종합병원": {"순환기(Cardiology IM)": {"2026-05": 90.0}}
    }


def test_general_aggregate_slices_ubist_channel_axis_from_raw_matrix() -> None:
    request = DynamicMarketRequest.model_validate(
        {
            "source": "ubist",
            "measure": "sales",
            "filters": {
                "atc4": ["C10A1"],
                "channel_axis": {
                    "ubist": {
                        "facility": ["종합병원"],
                        "specialty": ["순환기(Cardiology IM)"],
                    }
                },
            },
        }
    )
    aggregator = MetricAggregator(mart_db="jw_mart")
    rows = [
        {
            "brand_key": "a",
            "brand_name": "A",
            "atc4_code": "C10A1",
            "unit_label": "KRW",
            "raw_value_history": json.dumps({"2026-04": 100.0, "2026-05": 300.0}),
            "channel_specialty_matrix": json.dumps(
                {
                    "종합병원": {
                        "순환기(Cardiology IM)": {"2026-04": 30.0, "2026-05": 40.0},
                        "내분비(Endocrinology IM)": {"2026-05": 70.0},
                    },
                    "의원": {"분리되지 않은 내과": {"2026-05": 190.0}},
                },
                ensure_ascii=False,
            ),
        },
        {
            "brand_key": "b",
            "brand_name": "B",
            "atc4_code": "C10A1",
            "unit_label": "KRW",
            "raw_value_history": json.dumps({"2026-04": 50.0, "2026-05": 150.0}),
            "channel_specialty_matrix": json.dumps(
                {"종합병원": {"순환기(Cardiology IM)": {"2026-05": 60.0}}},
                ensure_ascii=False,
            ),
        },
    ]

    brand_metrics, monthly_totals = aggregator._aggregate_rows(
        rows,
        period_range=PeriodRange(),
        channel_axis=request.filters.channel_axis.to_filter(),
    )

    assert monthly_totals == {"2026-04": 30.0, "2026-05": 100.0}
    assert [item.total_value for item in brand_metrics] == [70.0, 60.0]
    assert brand_metrics[0].monthly_series == (
        {"period": "2026-04", "value": 30.0},
        {"period": "2026-05", "value": 40.0},
    )
    assert brand_metrics[0].channel_specialty_matrix == {
        "종합병원": {"순환기(Cardiology IM)": {"2026-04": 30.0, "2026-05": 40.0}}
    }


def test_sidecar_rows_keep_channel_matrix_for_channel_axis_slice() -> None:
    rows = [
        {
            "brand_key": "a",
            "brand_name": "A",
            "atc4_code": "C10A1",
            "product_code": "p1",
            "raw_value_history": json.dumps({"2026-05": 10.0}),
            "dimension_type": "seller",
        }
    ]
    matrix = {"종합병원": {"순환기(Cardiology IM)": {"2026-05": 10.0}}}

    metric_rows = sidecar_rows_to_metric_rows(
        rows,
        metadata={
            ("a", "C10A1"): {
                "unit_label": "KRW",
                "channel_specialty_matrix": matrix,
            }
        },
        required_dimensions=("seller",),
    )

    assert metric_rows == [
        {
            "brand_key": "a",
            "brand_name": "A",
            "atc4_code": "C10A1",
            "unit_label": "KRW",
            "raw_value_history": '{"2026-05": 10.0}',
            "channel_specialty_matrix": matrix,
        }
    ]


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


def test_dynamic_market_route_wraps_composer_payload_in_cause_envelope(monkeypatch) -> None:
    bare_payload = {
        "brand": "리바로",
        "source": "UBIST",
        "measure": "sales",
        "market_meta": {"market_id": "ml_005"},
        "data": {"kpi": {"market_size_recent": 100.0}},
    }

    class FakeAggregator:
        def __init__(self, **_: object) -> None:
            pass

        def aggregate(self, **_: object) -> object:
            return object()

    class FakeComposer:
        def compose(self, **_: object) -> dict:
            return dict(bare_payload)

    definition = SimpleNamespace(
        brands=(),
        source="ubist",
        measure="sales",
        dimension_filters=(),
        channel_axis=None,
        view="strategic_ml",
        strategic_market_id="ml_005",
    )
    monkeypatch.setattr(dynamic_market_route, "_resolve_definition", lambda payload: definition)
    monkeypatch.setattr(dynamic_market_route, "MetricAggregator", FakeAggregator)
    monkeypatch.setattr(dynamic_market_route, "ResponseComposer", FakeComposer)

    response = dynamic_market_route.dynamic_market(DynamicMarketRequest())

    assert response == {"status": "SUCCESS", "result": bare_payload}
    assert "data" not in response


def test_dynamic_market_route_allows_scope_at_brand_row_limit(monkeypatch) -> None:
    bare_payload = {
        "brand": "리바로",
        "source": "UBIST",
        "measure": "sales",
        "market_meta": {"market_id": "dynamic_general_safe"},
        "data": {"kpi": {"market_size_recent": 100.0}},
    }
    definition = SimpleNamespace(
        brands=(
            BrandRef("a", "A", "C10A1"),
            BrandRef("b", "B", "C10A1"),
        ),
        source="ubist",
        measure="sales",
        dimension_filters=(),
        channel_axis=None,
        view="general",
        strategic_market_id=None,
    )
    aggregate_calls: list[object] = []

    class FakeAggregator:
        def __init__(self, **_: object) -> None:
            pass

        def aggregate(self, **kwargs: object) -> object:
            aggregate_calls.append(kwargs)
            return object()

    class FakeComposer:
        def compose(self, **_: object) -> dict:
            return dict(bare_payload)

    monkeypatch.setattr(
        dynamic_market_route,
        "config",
        SimpleNamespace(db_name="jw_mart", strategic_dimension_db_name="jw_mart", dynamic_max_brand_rows=2),
    )
    monkeypatch.setattr(dynamic_market_route, "_resolve_definition", lambda payload: definition)
    monkeypatch.setattr(dynamic_market_route, "MetricAggregator", FakeAggregator)
    monkeypatch.setattr(dynamic_market_route, "ResponseComposer", FakeComposer)

    response = dynamic_market_route.dynamic_market(DynamicMarketRequest())

    assert response == {"status": "SUCCESS", "result": bare_payload}
    assert len(aggregate_calls) == 1


def test_dynamic_market_route_rejects_scope_before_aggregation_when_brand_row_limit_exceeded(monkeypatch) -> None:
    definition = SimpleNamespace(
        brands=(
            BrandRef("a", "A", "C10A1"),
            BrandRef("b", "B", "C10A1"),
            BrandRef("c", "C", "C10A1"),
        ),
        source="ubist",
        measure="sales",
        dimension_filters=(),
        channel_axis=None,
        view="general",
        strategic_market_id=None,
    )

    class FakeAggregator:
        def __init__(self, **_: object) -> None:
            pass

        def aggregate(self, **_: object) -> object:
            raise AssertionError("scope guard must reject before aggregation")

    monkeypatch.setattr(
        dynamic_market_route,
        "config",
        SimpleNamespace(db_name="jw_mart", strategic_dimension_db_name="jw_mart", dynamic_max_brand_rows=2),
    )
    monkeypatch.setattr(dynamic_market_route, "_resolve_definition", lambda payload: definition)
    monkeypatch.setattr(dynamic_market_route, "MetricAggregator", FakeAggregator)

    with pytest.raises(dynamic_market_route.HTTPException) as exc_info:
        dynamic_market_route.dynamic_market(DynamicMarketRequest())

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == {
        "error": "dynamic_scope_too_broad",
        "message": "시장 범위가 너무 넓습니다. 범위를 좁혀주세요.",
        "resolved_brand_rows": 3,
        "limit": 2,
    }


def test_compose_emits_only_portal_read_cause_sections() -> None:
    definition = MarketDefinition(
        view="general",
        filter_echo={"view": "general", "atc4": ["C10B"], "molecule": [], "source": "ubist", "measure": "sales"},
        source="ubist",
        measure="sales",
    )
    brand = BrandMetric(
        "focus",
        "Focus Brand",
        "C10B",
        100.0,
        100.0,
        1,
        "2026-04",
        100.0,
        ({"period": "2026-04", "value": 100.0},),
    )
    metrics = AggregatedMetrics(
        source="ubist",
        measure="sales",
        unit_label="KRW",
        market_size=100.0,
        hhi=None,
        cagr=None,
        monthly_series=({"period": "2026-04", "market_size": 100.0},),
        brands=(brand,),
        all_brands=(brand,),
    )

    payload = build_cause_payload(definition=definition, metrics=metrics)

    data = payload["data"]
    assert "atc_desc" not in payload["market_meta"]
    assert "data_period_coverage" not in data
    assert "resolved_scope" not in payload
    assert {"market_size_recent", "target_share_pct", "target_rank", "market_cagr_5y_pct", "direct_competition_count"}.issubset(
        data["kpi"]
    )
    assert {"market_size_series", "hhi_series_5y"}.issubset(data["sources_data"])
    assert {"years", "yearly"}.issubset(data["brand_ranking_stacked"])
    assert {"years", "yearly"}.issubset(data["company_ranking_stacked"])
    assert {"levels", "channels", "data"}.issubset(data["analysis_levels"])
    assert {"available_levels", "default_level", "by_level"}.issubset(data["level_top5_trend"])
    assert {"data"}.issubset(data["ei_ms_matrix"])
    assert {"data"}.issubset(data["growth_contribution_ms_matrix"])
    assert {"targets"}.issubset(data["target_customer_competition"])


def test_cause_payload_keeps_requested_focus_brand_visible_when_it_is_outside_top5() -> None:
    definition = MarketDefinition(
        view="strategic_ml",
        filter_echo={"view": "strategic_ml", "ml_id": "ml_005", "source": "ubist", "measure": "sales"},
        source="ubist",
        measure="sales",
        focus_brand_key="focus",
        strategic_market_kind="ml",
        strategic_market_id="ml_005",
    )
    brands = tuple(
        BrandMetric(
            f"brand-{index}",
            f"Brand {index}",
            "",
            float(100 - index),
            0.0,
            index,
            "2026-04",
            float(100 - index),
            ({"period": "2026-04", "value": float(100 - index)},),
        )
        for index in range(1, 6)
    ) + (
        BrandMetric(
            "focus",
            "Focus Brand",
            "",
            1.0,
            0.0,
            6,
            "2026-04",
            1.0,
            ({"period": "2026-04", "value": 1.0},),
        ),
    )
    metrics = AggregatedMetrics(
        source="ubist",
        measure="sales",
        unit_label="KRW",
        market_size=486.0,
        hhi=None,
        cagr=None,
        monthly_series=({"period": "2026-04", "market_size": 486.0},),
        brands=brands[:5],
        all_brands=brands,
    )

    payload = build_cause_payload(definition=definition, metrics=metrics)

    ranking = payload["data"]["brand_ranking"]["rankings_by_year"]["2026"]
    focus_rows = [row for row in ranking if row["brand"] == "Focus Brand"]
    assert payload["brand_key"] == "focus"
    assert payload["data"]["kpi"]["target_brand"] == "Focus Brand"
    assert focus_rows == [
        {
            "brand": "Focus Brand",
            "company": "Focus Brand",
            "rank": 6,
            "value": 1.0,
            "ms_pct": 1.0 / 486.0 * 100,
            "is_target": True,
            "is_jw": False,
            "is_others": False,
        }
    ]


def test_cause_payload_hhi_recent_uses_complete_calendar_year_not_partial_latest_month() -> None:
    definition = MarketDefinition(
        view="strategic_ml",
        filter_echo={"view": "strategic_ml", "ml_id": "ml_005", "source": "ubist", "measure": "sales"},
        source="ubist",
        measure="sales",
        strategic_market_kind="ml",
        strategic_market_id="ml_005",
    )
    complete_year_a = tuple({"period": f"2025-{month:02d}", "value": 75.0} for month in range(1, 13))
    complete_year_b = tuple({"period": f"2025-{month:02d}", "value": 25.0} for month in range(1, 13))
    partial_year_a = tuple({"period": f"2026-{month:02d}", "value": 10.0} for month in range(1, 5))
    partial_year_b = tuple({"period": f"2026-{month:02d}", "value": 90.0} for month in range(1, 5))
    brands = (
        BrandMetric("a", "A", "", 940.0, 0.0, 1, "2026-04", 10.0, complete_year_a + partial_year_a),
        BrandMetric("b", "B", "", 660.0, 0.0, 2, "2026-04", 90.0, complete_year_b + partial_year_b),
    )
    metrics = AggregatedMetrics(
        source="ubist",
        measure="sales",
        unit_label="KRW",
        market_size=1600.0,
        hhi=None,
        cagr=None,
        monthly_series=tuple({"period": f"2025-{month:02d}", "market_size": 100.0} for month in range(1, 13))
        + tuple({"period": f"2026-{month:02d}", "market_size": 100.0} for month in range(1, 5)),
        brands=brands,
        all_brands=brands,
    )

    payload = build_cause_payload(definition=definition, metrics=metrics)

    assert payload["data"]["hhi_series_5y"] == [{"period": "2025", "period_full": "2025", "year": 2025, "hhi": 6250.0}]
    assert payload["data"]["kpi"]["hhi_recent"] == 6250.0


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


def test_general_resolver_omits_inactive_channel_axis_from_identity_echo(monkeypatch) -> None:
    def fake_fetch_all(sql: str, params: tuple[str, ...]) -> list[dict]:
        assert "channel_axis" not in str(sql).lower()
        assert params == ["ubist", "sales", "C10A1"]
        return [{"brand_key": "livaro", "brand_name": "리바로", "atc4_code": "C10A1"}]

    monkeypatch.setattr(resolvers.db, "fetch_all", fake_fetch_all)

    definition = GeneralViewResolver(mart_db="jw_mart", bridge_db="jw_mart").resolve(
        atc4=["C10A1"],
        molecule=[],
        source="ubist",
        measure="sales",
        channel_axis=None,
    )

    assert "channel_axis" not in definition.filter_echo


def test_empty_channel_axis_payloads_normalize_like_missing_filter() -> None:
    payloads = [
        {"filters": {"atc4": ["C10A1"]}, "source": "ubist", "measure": "sales"},
        {"filters": {"atc4": ["C10A1"], "channel_axis": {}}, "source": "ubist", "measure": "sales"},
        {"filters": {"atc4": ["C10A1"], "channel_axis": {"ubist": {}}}, "source": "ubist", "measure": "sales"},
        {"filters": {"atc4": ["C10A1"], "channel_axis": {"ubist": {"facility": []}}}, "source": "ubist", "measure": "sales"},
        {"filters": {"atc4": ["C10A1"], "channel_axis": {"ubist": {"specialty": [], "pairs": []}}}, "source": "ubist", "measure": "sales"},
    ]

    for payload in payloads:
        request = DynamicMarketRequest.model_validate(payload)
        assert request.filters.channel_axis.to_filter(source=request.source) is None


def test_empty_iqvia_channel_axis_payloads_normalize_like_missing_filter() -> None:
    payloads = [
        {"filters": {"atc4": ["C10A1"]}, "source": "iqvia", "measure": "sales"},
        {"filters": {"atc4": ["C10A1"], "channel_axis": {}}, "source": "iqvia", "measure": "sales"},
        {"filters": {"atc4": ["C10A1"], "channel_axis": {"iqvia": {}}}, "source": "iqvia", "measure": "sales"},
        {"filters": {"atc4": ["C10A1"], "channel_axis": {"iqvia": {"audit_code": []}}}, "source": "iqvia", "measure": "sales"},
    ]

    for payload in payloads:
        request = DynamicMarketRequest.model_validate(payload)
        assert request.filters.channel_axis.to_filter(source=request.source) is None


def test_channel_axis_rejects_source_mismatch() -> None:
    request = DynamicMarketRequest.model_validate(
        {
            "filters": {
                "atc4": ["C10A1"],
                "channel_axis": {"iqvia": {"audit_code": ["KPA"]}},
            },
            "source": "ubist",
            "measure": "sales",
        }
    )

    try:
        request.filters.channel_axis.to_filter(source=request.source)
    except ValueError as exc:
        assert "channel_axis.iqvia must match selected source" in str(exc)
    else:
        raise AssertionError("source-mismatched channel_axis must be rejected")


def test_route_returns_envelope_for_general_dynamic_market(monkeypatch) -> None:
    class FakeResolver:
        def __init__(self, *, mart_db: str, bridge_db: str) -> None:
            assert mart_db
            assert bridge_db

        def resolve(self, *, atc4, molecule, source, measure, **_kwargs):
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
        def __init__(self, *, mart_db: str, strategic_dimension_db: str | None = None) -> None:
            assert mart_db
            assert strategic_dimension_db

        def aggregate(self, *, brands, source, measure, period_range, top_n, **_kwargs):
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


def test_route_passes_general_channel_axis_to_aggregator(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeResolver:
        def __init__(self, *, mart_db: str, bridge_db: str) -> None:
            assert mart_db
            assert bridge_db

        def resolve(self, *, atc4, molecule, source, measure, channel_axis, **_kwargs):
            captured["resolver_channel_axis"] = channel_axis
            return MarketDefinition(
                view="general",
                filter_echo={
                    "view": "general",
                    "atc4": list(atc4),
                    "molecule": list(molecule),
                    "source": source,
                    "measure": measure,
                    "channel_axis": {"facility": ["종합병원"]},
                },
                source=source,
                measure=measure,
                brands=(),
                channel_axis=channel_axis,
            )

    class FakeAggregator:
        def __init__(self, *, mart_db: str, strategic_dimension_db: str | None = None) -> None:
            assert mart_db
            assert strategic_dimension_db

        def aggregate(self, *, channel_axis, **_kwargs):
            captured["aggregator_channel_axis"] = channel_axis
            return AggregatedMetrics(
                source="ubist",
                measure="sales",
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
        DynamicMarketRequest.model_validate(
            {
                "source": "ubist",
                "measure": "sales",
                "filters": {
                    "atc4": ["C10A1"],
                    "channel_axis": {"ubist": {"facility": ["종합병원"]}},
                },
            }
        )
    )

    assert response["status"] == "SUCCESS"
    assert captured["resolver_channel_axis"].facilities == ("종합병원",)
    assert captured["aggregator_channel_axis"].facilities == ("종합병원",)


def test_route_passes_iqvia_channel_axis_to_aggregator(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeResolver:
        def __init__(self, *, mart_db: str, bridge_db: str) -> None:
            assert mart_db
            assert bridge_db

        def resolve(self, *, atc4, molecule, source, measure, channel_axis, **_kwargs):
            captured["resolver_channel_axis"] = channel_axis
            return MarketDefinition(
                view="general",
                filter_echo={
                    "view": "general",
                    "atc4": list(atc4),
                    "molecule": list(molecule),
                    "source": "iqvia_nsa",
                    "measure": measure,
                    "channel_axis": {"source": "iqvia_nsa", "audit_code": ["KPA"]},
                },
                source="iqvia_nsa",
                measure=measure,
                brands=(),
                channel_axis=channel_axis,
            )

    class FakeAggregator:
        def __init__(self, *, mart_db: str, strategic_dimension_db: str | None = None) -> None:
            assert mart_db
            assert strategic_dimension_db

        def aggregate(self, *, channel_axis, **_kwargs):
            captured["aggregator_channel_axis"] = channel_axis
            return AggregatedMetrics(
                source="iqvia_nsa",
                measure="sales",
                unit_label="KRW",
                market_size=1.0,
                hhi=None,
                cagr=None,
                monthly_series=({"period": "2026-Q1", "market_size": 1.0},),
                brands=(),
            )

    monkeypatch.setattr(dynamic_market_route, "GeneralViewResolver", FakeResolver)
    monkeypatch.setattr(dynamic_market_route, "MetricAggregator", FakeAggregator)

    response = dynamic_market_route.dynamic_market(
        DynamicMarketRequest.model_validate(
            {
                "source": "iqvia",
                "measure": "sales",
                "filters": {
                    "atc4": ["C10A1"],
                    "channel_axis": {"iqvia": {"audit_code": ["KPA"]}},
                },
            }
        )
    )

    assert response["status"] == "SUCCESS"
    assert captured["resolver_channel_axis"].source == "iqvia_nsa"
    assert captured["resolver_channel_axis"].audit_codes == ("KPA",)
    assert captured["aggregator_channel_axis"].audit_codes == ("KPA",)


def test_route_rejects_channel_axis_for_strategic_view() -> None:
    try:
        dynamic_market_route.dynamic_market(
            DynamicMarketRequest.model_validate(
                {
                    "source": "iqvia",
                    "measure": "sales",
                    "filters": {
                        "ml_id": "ml_006",
                        "channel_axis": {"iqvia": {"audit_code": ["KPA"]}},
                    },
                }
            )
        )
    except dynamic_market_route.HTTPException as exc:
        assert exc.status_code == 400
        assert "channel_axis is supported only for general views" in str(exc.detail)
    else:
        raise AssertionError("strategic channel_axis must be rejected")


def test_iqvia_channel_axis_response_adds_selected_audit_summary_only_when_active() -> None:
    definition = MarketDefinition(
        view="general",
        filter_echo={"view": "general", "atc4": ["C10A1"], "source": "iqvia_nsa", "measure": "sales"},
        source="iqvia_nsa",
        measure="sales",
    )
    inactive_metrics = AggregatedMetrics(
        source="iqvia_nsa",
        measure="sales",
        unit_label="KRW",
        market_size=100.0,
        hhi=None,
        cagr=None,
        monthly_series=({"period": "2025-Q4", "market_size": 100.0},),
        brands=(),
        all_brands=(
            BrandMetric(
                "livaro",
                "리바로",
                "C10A1",
                100.0,
                100.0,
                1,
                "2025-Q4",
                100.0,
                audit_code_matrix={"KPA": {"2025-Q4": 100.0}},
            ),
        ),
    )

    inactive_payload = cause_payload.build_cause_data(definition=definition, metrics=inactive_metrics, focus=None)

    assert "iqvia_audit_code_channels" not in inactive_payload

    active_definition = MarketDefinition(
        view="general",
        filter_echo={
            "view": "general",
            "atc4": ["C10A1"],
            "source": "iqvia_nsa",
            "measure": "sales",
            "channel_axis": {"source": "iqvia_nsa", "audit_code": ["KPA"]},
        },
        source="iqvia_nsa",
        measure="sales",
        channel_axis=DynamicMarketRequest.model_validate(
            {
                "source": "iqvia",
                "measure": "sales",
                "filters": {"atc4": ["C10A1"], "channel_axis": {"iqvia": {"audit_code": ["KPA"]}}},
            }
        ).filters.channel_axis.to_filter(source="iqvia"),
    )

    active_payload = cause_payload.build_cause_data(definition=active_definition, metrics=inactive_metrics, focus=None)

    assert active_payload["iqvia_audit_code_channels"] == [
        {"audit_code": "KPA", "latest_period": "2025-Q4", "latest_value": 100.0, "total_value": 100.0}
    ]


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
        metadata={("brand-a", "A10A1"): {"unit_label": "KRW"}},
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
        return [{"brand_key": "brand-a", "atc4_code": "A10A1", "unit_label": "KRW"}]

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


def test_strategic_resolver_requires_view_specific_market_id() -> None:
    resolver = StrategicViewResolver(mart_db="jw_mart")

    try:
        resolver.resolve(
            view_kind="market_landscape",
            ml_id=None,
            cd_market_id=None,
            atc4=[],
            molecule=[],
            analysis_level=None,
            focus_brand_key=None,
            source="ubist",
            measure="sales",
        )
    except DynamicMarketInputError as exc:
        assert "ml_id" in str(exc)
    else:
        raise AssertionError("strategic market_landscape accepted a missing ml_id")


def test_dynamic_route_resolves_brand_only_market_landscape_from_catalog(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeStrategicResolver:
        def __init__(self, **_: object) -> None:
            pass

        def resolve(self, **kwargs: object) -> MarketDefinition:
            captured.update(kwargs)
            return MarketDefinition(
                view="strategic_ml",
                filter_echo={"view": "strategic_ml", "ml_id": kwargs["ml_id"]},
                source="ubist",
                measure="sales",
                strategic_market_kind="ml",
                strategic_market_id=str(kwargs["ml_id"]),
            )

    monkeypatch.setattr(dynamic_market_route, "StrategicViewResolver", FakeStrategicResolver)
    payload = DynamicMarketRequest.model_validate(
        {
            "filters": {
                "view_kind": "market_landscape",
                "focus_brand_key": "리바로",
            },
            "source": "ubist",
            "measure": "sales",
        }
    )

    definition = dynamic_market_route._resolve_definition(payload)

    assert captured["ml_id"] == "ml_006"
    assert definition.strategic_market_id == "ml_006"


def test_dynamic_route_keeps_explicit_market_id_ahead_of_catalog(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeStrategicResolver:
        def __init__(self, **_: object) -> None:
            pass

        def resolve(self, **kwargs: object) -> MarketDefinition:
            captured.update(kwargs)
            return MarketDefinition(
                view="strategic_ml",
                filter_echo={"view": "strategic_ml", "ml_id": kwargs["ml_id"]},
                source="ubist",
                measure="sales",
                strategic_market_kind="ml",
                strategic_market_id=str(kwargs["ml_id"]),
            )

    monkeypatch.setattr(dynamic_market_route, "StrategicViewResolver", FakeStrategicResolver)
    payload = DynamicMarketRequest.model_validate(
        {
            "filters": {
                "view_kind": "market_landscape",
                "ml_id": "ml_005",
                "focus_brand_key": "리바로",
            },
            "source": "ubist",
            "measure": "sales",
        }
    )

    definition = dynamic_market_route._resolve_definition(payload)

    assert captured["ml_id"] == "ml_005"
    assert definition.strategic_market_id == "ml_005"


def test_strategic_resolver_uses_cd_table_for_competitive_dynamics(monkeypatch) -> None:
    calls: list[str] = []

    def fake_fetch_all(sql: str, params: tuple[str, ...]) -> list[dict]:
        calls.append(sql)
        assert params[:3] == ("cd_002", "iqvia_nsa", "sales")
        return [{"brand_key": "brand-cd", "brand_name": "CD Brand", "atc4_code": ""}]

    monkeypatch.setattr("pipeline.scripts.api.dynamic_market.resolvers.db.fetch_all", fake_fetch_all)
    definition = StrategicViewResolver(mart_db="jw_mart").resolve(
        view_kind="competitive_dynamics",
        ml_id=None,
        cd_market_id="cd_002",
        atc4=[],
        molecule=[],
        analysis_level=None,
        focus_brand_key=None,
        source="iqvia",
        measure="sales",
    )

    assert definition.view == "strategic_cd"
    assert definition.filter_echo["cd_market_id"] == "cd_002"
    assert definition.brands == (BrandRef("brand-cd", "CD Brand", ""),)
    assert any("mart_strategic_cd_brand_metric" in sql for sql in calls)


def test_strategic_sidecar_aggregation_keeps_recode_product_history(monkeypatch) -> None:
    calls: list[str] = []

    def fake_fetch_all(sql: str, params: tuple[str, ...]) -> list[dict]:
        calls.append(sql)
        if "mart_strategic_filter_dimension_metric" in sql:
            assert params[:4] == ("ml", "ml_005", "ubist", "sales")
            return [
                {
                    "brand_key": "미케란",
                    "brand_name": "미케란",
                    "product_code": "649900100",
                    "dimension_type": "seller",
                    "raw_value_history": json.dumps({"2026-04": 31_282_626.06}),
                }
            ]
        if "mart_strategic_ml_brand_metric" in sql:
            return [{"brand_key": "미케란", "unit_label": "KRW"}]
        raise AssertionError(sql)

    monkeypatch.setattr("pipeline.scripts.api.dynamic_market.aggregator.db.fetch_all", fake_fetch_all)
    metrics = MetricAggregator(mart_db="jw_mart").aggregate(
        brands=(BrandRef("미케란", "미케란", ""),),
        source="ubist",
        measure="sales",
        period_range=PeriodRange("2026-04", "2026-04"),
        top_n=20,
        dimension_filters=(DimensionFilter("seller", ("태준제약",)),),
        view="strategic_ml",
        strategic_market_id="ml_005",
    )

    assert metrics.market_size == 31_282_626.06
    assert metrics.brands[0].total_value == 31_282_626.06
    assert any("mart_strategic_filter_dimension_metric" in sql for sql in calls)
    assert not any("mart_general_filter_dimension_metric" in sql for sql in calls)
