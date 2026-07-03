from __future__ import annotations

import json
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.scripts.api.dynamic_market.aggregator import MetricAggregator, compute_hhi, sidecar_rows_to_metric_rows
from pipeline.scripts.api.dynamic_market import cause_payload
from pipeline.scripts.api.dynamic_market.composer import ResponseComposer
from pipeline.scripts.api.dynamic_market import resolvers
from pipeline.scripts.api.dynamic_market.resolvers import GeneralViewResolver, expand_atc4_for_source
from pipeline.scripts.api.dynamic_market import strategic_runtime
from pipeline.scripts.etl import build_cache_cause as cause_builder
from pipeline.scripts.api.dynamic_market.types import AggregatedMetrics, BrandMetric, BrandRef, MarketDefinition, PeriodRange
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


def test_general_aggregate_reads_raw_matrix_without_derived_channel_columns(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_fetch_all(sql, params):
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


def test_general_aggregate_reads_audit_code_matrix_for_iqvia(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_fetch_all(sql, params):
        captured["sql"] = sql
        captured["params"] = params
        return [
            {
                "brand_key": "livaro",
                "brand_name": "리바로",
                "atc4_code": "C10A1",
                "source": "iqvia_nsa",
                "measure": "sales",
                "unit_label": "KRW",
                "raw_value_history": json.dumps({"2025-Q4": 100.0}),
                "channel_specialty_matrix": "{}",
                "audit_code_matrix": json.dumps({"KPA": {"2025-Q4": 90.0}}),
            }
        ]

    monkeypatch.setattr("pipeline.scripts.api.dynamic_market.aggregator.db.fetch_all", fake_fetch_all)

    metrics = MetricAggregator(mart_db="jw_mart").aggregate(
        brands=(BrandRef("livaro", "리바로", "C10A1"),),
        source="iqvia_nsa",
        measure="sales",
        period_range=PeriodRange(),
        top_n=20,
    )

    assert "audit_code_matrix" in str(captured["sql"])
    assert captured["params"] == ("iqvia_nsa", "sales", "livaro", "C10A1")
    assert metrics.all_brands[0].audit_code_matrix == {"KPA": {"2025-Q4": 90.0}}


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


def test_general_aggregate_slices_iqvia_audit_code_axis_from_matrix() -> None:
    request = DynamicMarketRequest.model_validate(
        {
            "source": "iqvia",
            "measure": "sales",
            "filters": {
                "atc4": ["C10A1"],
                "channel_axis": {"iqvia": {"audit_code": ["KPA", "KHPA"]}},
            },
        }
    )
    aggregator = MetricAggregator(mart_db="jw_mart")
    rows = [
        {
            "brand_key": "livaro",
            "brand_name": "리바로",
            "atc4_code": "C10A1",
            "unit_label": "KRW",
            "raw_value_history": json.dumps({"2025-Q4": 1000.0, "2026-Q1": 2000.0}),
            "audit_code_matrix": json.dumps(
                {
                    "KPA": {"2025-Q4": 100.0, "2026-Q1": 200.0},
                    "KHPA": {"2026-Q1": 30.0},
                    "KCPA": {"2026-Q1": 900.0},
                }
            ),
        },
        {
            "brand_key": "competitor",
            "brand_name": "경쟁",
            "atc4_code": "C10A1",
            "unit_label": "KRW",
            "raw_value_history": json.dumps({"2025-Q4": 500.0, "2026-Q1": 500.0}),
            "audit_code_matrix": json.dumps({"KPA": {"2026-Q1": 70.0}, "KCPA": {"2026-Q1": 430.0}}),
        },
    ]

    brand_metrics, monthly_totals = aggregator._aggregate_rows(
        rows,
        period_range=PeriodRange(),
        channel_axis=request.filters.channel_axis.to_filter(source=request.source),
    )

    assert monthly_totals == {"2025-Q4": 100.0, "2026-Q1": 300.0}
    assert [item.total_value for item in brand_metrics] == [330.0, 70.0]
    assert brand_metrics[0].monthly_series == (
        {"period": "2025-Q4", "value": 100.0},
        {"period": "2026-Q1", "value": 230.0},
    )
    assert brand_metrics[0].audit_code_matrix == {
        "KPA": {"2025-Q4": 100.0, "2026-Q1": 200.0},
        "KHPA": {"2026-Q1": 30.0},
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
                200.0,
                66.666667,
                1,
                "2026-01",
                150.0,
                ({"period": "2025-01", "value": 50.0}, {"period": "2026-01", "value": 150.0}),
                ubist_channel_by_code={"GH Cardio": {"2026-01": 120.0}, "CL IGF": {"2026-01": 30.0}},
            ),
            BrandMetric(
                "other",
                "경쟁",
                "C10B",
                100.0,
                33.333333,
                2,
                "2026-01",
                50.0,
                ({"period": "2025-01", "value": 50.0}, {"period": "2026-01", "value": 50.0}),
                ubist_channel_by_code={"GH Cardio": {"2026-01": 50.0}, "GH Endo": {"2026-01": 25.0}},
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
    assert response["markets"] == [{"market_id": response["market_id"], "is_primary": True}]
    assert data["ubist_specialty_channels"] == ["전체", "종합병원 순환기", "의원 IGF", "종합병원 내분비"]
    assert data["ubist_specialty_target_channels"][0]["code"] == "GH Cardio"
    assert data["ubist_specialty_target_channels"][0]["facility_raw_values"] == ["상급종합병원", "종합병원", "병원"]


def test_general_ubist_channels_rank_latest_raw_matrix_before_legacy_totals() -> None:
    metrics = AggregatedMetrics(
        source="ubist",
        measure="sales",
        unit_label="KRW",
        market_size=0.0,
        hhi=None,
        cagr=None,
        monthly_series=(),
        brands=(),
        all_brands=(
            BrandMetric(
                brand_key="a",
                brand_name="A",
                atc4_code="C10C0",
                total_value=0.0,
                market_share_pct=0.0,
                rank=1,
                latest_period="2026-05",
                latest_value=0.0,
                channel_specialty_matrix={
                    "종합병원": {
                        "신장(Nephrology IM)": {"2025-01": 10_000.0, "2026-05": 10.0},
                        "순환기(Cardiology IM)": {"2026-05": 90.0},
                    },
                    "의원": {"분리되지 않은 내과": {"2026-05": 80.0}},
                },
                ubist_channel_by_code={"GH Nephro": {"2025-01": 10_000.0, "2026-05": 10.0}},
            ),
        ),
    )

    channels = cause_payload._general_ubist_channels(metrics, max_channels=2)

    assert channels["specialty_channels"] == ["전체", "종합병원 순환기", "의원 IGF"]


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


def test_ubist_atc4_alias_expansion_reverses_canonical_padding() -> None:
    assert expand_atc4_for_source(("C10C0", "A02B1", "A02X0", "C10A1"), source="ubist") == (
        "C10C0",
        "C10C",
        "A02B1",
        "A2B1",
        "A02X0",
        "A2X0",
        "A02X",
        "A2X",
        "C10A1",
    )


def test_iqvia_atc4_alias_expansion_keeps_canonical_codes() -> None:
    assert expand_atc4_for_source(("C10C0", "A02B1"), source="iqvia_nsa") == ("C10C0", "A02B1")


def test_general_resolver_expands_ubist_canonical_atc4_for_source_native_rows(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_fetch_all(sql, params):
        captured["sql"] = sql
        captured["params"] = params
        return [{"brand_key": "rosuzet", "brand_name": "로수젯", "atc4_code": "C10C"}]

    monkeypatch.setattr(resolvers.db, "fetch_all", fake_fetch_all)

    definition = GeneralViewResolver(mart_db="jw_mart", bridge_db="jw_mart").resolve(
        atc4=["C10C0"],
        molecule=[],
        source="ubist",
        measure="sales",
    )

    assert "atc4_code IN (%s, %s)" in str(captured["sql"])
    assert captured["params"] == ["ubist", "sales", "C10C0", "C10C"]
    assert definition.filter_echo["atc4"] == ["C10C0"]
    assert [brand.atc4_code for brand in definition.brands] == ["C10C"]


def test_general_resolver_omits_inactive_channel_axis_from_identity_echo(monkeypatch) -> None:
    def fake_fetch_all(sql, params):
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
        def __init__(self, *, mart_db: str) -> None:
            assert mart_db

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
        def __init__(self, *, mart_db: str) -> None:
            assert mart_db

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
        def __init__(self, *, mart_db: str) -> None:
            assert mart_db

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


def test_route_rejects_channel_axis_for_strategic_shortcut() -> None:
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
            "ubist_channel_by_display": json.dumps({"종합병원 순환기": {"2026-04": 50.0}}),
            "ubist_channel_by_code": json.dumps({"GH Cardio": {"2026-04": 50.0}}),
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
            "ubist_channel_by_display": json.dumps({"종합병원 순환기": {"2026-04": 75.0}}),
            "ubist_channel_by_code": json.dumps({"GH Cardio": {"2026-04": 75.0}}),
        },
    ]
    captured: dict[str, object] = {}

    def fake_fetch_all(sql, params):
        if "mart_general_brand_metric" in sql:
            assert params == ["ubist", "sales", "경쟁", "리바로젯", "C10C"]
            return [
                {
                    "brand_key": "리바로젯",
                    "channel_specialty_matrix": json.dumps(
                        {"종합병원": {"순환기(Cardiology IM)": {"2026-04": 50.0}}},
                        ensure_ascii=False,
                    ),
                },
                {
                    "brand_key": "경쟁",
                    "channel_specialty_matrix": json.dumps(
                        {"종합병원": {"순환기(Cardiology IM)": {"2026-04": 75.0}}},
                        ensure_ascii=False,
                    ),
                },
            ]
        assert "mart_strategic_ml_brand_metric" in sql
        assert params == ["ml_006", "ubist", "sales"]
        return brand_rows

    def fake_fetch_one(sql, params):
        assert "mart_strategic_ml_market_metric" in sql
        assert params == ["ml_006", "ubist", "sales"]
        return market_row

    def fake_build_response(**kwargs):
        channel_context = strategic_runtime.cause_builder.resolve_market_channels(
            rows=kwargs["sibling_rows"],
            market={"target_ubist_1": "GH Cardio"},
            measure=kwargs["measure"],
        )
        captured.update(kwargs)
        captured["channel_context"] = channel_context
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
    assert result["markets"] == [{"market_id": "strategy_006", "is_primary": True}]
    assert captured["brand_row"]["brand_name"] == "리바로젯"
    assert captured["market_id"] == "strategy_006"
    assert captured["source"] == "UBIST"
    assert captured["channel_context"]["specialty_channels"] == ["전체", "주요고객 종합병원 순환기"]


def test_strategic_runtime_catalog_reads_from_db(monkeypatch) -> None:
    strategic_runtime._ml_market_catalog.cache_clear()
    strategic_runtime._cd_market_catalog.cache_clear()
    strategic_runtime._strategic_brand_catalog.cache_clear()
    queries: list[tuple[str, object]] = []

    def fail_load_catalog(name: str):
        raise AssertionError(f"runtime must not read parquet catalog: {name}")

    def fake_fetch_all(sql, params=None):
        queries.append((sql, params))
        if "catalog_ml_market" in sql:
            return [
                {
                    "ml_id": "ml_006",
                    "name": "리바로 리바로젯",
                    "data_source": "ubist",
                    "atc_codes_json": json.dumps(["C10A1", "C10C"]),
                    "analyze_class": 1,
                    "analyze_molecule": 1,
                    "analyze_strength_pack": 1,
                    "analyze_ox_gx": 1,
                }
            ]
        if "catalog_cd_market" in sql:
            return [
                {
                    "cd_id": "cd_006",
                    "ml_id": "ml_006",
                    "name": "리바로 CD",
                    "data_source": "ubist",
                }
            ]
        if "catalog_strategic_brand" in sql:
            return [
                {
                    "brand_id": "sb_006_리바로",
                    "ml_id": "ml_006",
                    "cd_id": "cd_006",
                    "canonical_name": "리바로",
                    "name": "리바로",
                    "is_jw": 1,
                    "판매사": "JW중외제약",
                }
            ]
        raise AssertionError(sql)

    monkeypatch.setattr(strategic_runtime.cause_builder, "load_catalog", fail_load_catalog)
    monkeypatch.setattr(strategic_runtime.db, "fetch_all", fake_fetch_all)

    ml_catalog = strategic_runtime._ml_market_catalog()
    cd_catalog = strategic_runtime._cd_market_catalog()
    strategic_brand = strategic_runtime._strategic_brand_catalog()

    assert ml_catalog["ml_006"]["analyze_class"] == 1
    assert cd_catalog["cd_006"]["cd_market_id"] == "cd_006"
    assert strategic_brand == [
        {
            "brand_id": "sb_006_리바로",
            "ml_id": "ml_006",
            "cd_id": "cd_006",
            "canonical_name": "리바로",
            "name": "리바로",
            "is_jw": 1,
            "판매사": "JW중외제약",
        }
    ]
    assert len(queries) == 3


def test_catalog_members_for_market_accepts_db_rows() -> None:
    members = cause_builder._catalog_members_for_market(
        [
            {
                "ml_id": "ml_006",
                "cd_id": "cd_006",
                "canonical_name": "리바로",
                "name": "리바로",
                "is_jw": 1,
                "판매사": "JW중외제약",
            },
            {
                "ml_id": "ml_003",
                "cd_id": "cd_003",
                "canonical_name": "가드렛",
                "name": "가드렛",
                "is_jw": 1,
                "판매사": "JW중외제약",
            },
        ],
        "ml_006",
    )

    assert members == [{"name": "리바로", "is_jw": True, "company": "JW중외제약"}]
