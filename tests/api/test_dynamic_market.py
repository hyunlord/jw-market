from __future__ import annotations

import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.scripts.api.dynamic_market import aggregator as aggregator_module
from pipeline.scripts.api.dynamic_market import cause_payload, cause_time, resolvers
from pipeline.scripts.api.dynamic_market.aggregator import (
    MetricAggregator,
    collect_ubist_channel_latest_totals,
    compute_cagr,
    compute_hhi,
)
from pipeline.scripts.api.dynamic_market.aggregator import sidecar_rows_to_metric_rows
from pipeline.scripts.api.dynamic_market.composer import ResponseComposer
from pipeline.scripts.api.dynamic_market.cause_sections import display_matrix_rows
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


def test_general_aggregate_omits_matrix_columns_when_channel_axis_is_inactive(monkeypatch) -> None:
    calls: list[str] = []

    def fake_fetch_all(sql: str, params: tuple[object, ...]) -> list[dict[str, object]]:
        calls.append(sql)
        assert "ubist_channel_by_display" not in sql
        assert "audit_code_matrix" not in sql
        assert "channel_specialty_matrix" not in sql
        return []

    def fake_iter_rows(sql: str, params: tuple[object, ...]):
        calls.append(sql)
        assert "ubist_channel_by_display" not in sql
        assert "audit_code_matrix" not in sql
        if "channel_specialty_matrix" in sql and "raw_value_history" not in sql:
            yield {
                "brand_key": "a",
                "atc4_code": "C10A1",
                "channel_specialty_matrix": json.dumps(
                    {
                        "종합병원": {"순환기(Cardiology IM)": {"2026-05": 90.0}},
                        "의원": {"가정의학과(FM)": {"2026-05": 10.0}},
                    },
                    ensure_ascii=False,
                )
            }
            return
        assert "channel_specialty_matrix" not in sql
        yield {
            "brand_key": "a",
            "brand_name": "A",
            "atc4_code": "C10A1",
            "source": "ubist",
            "measure": "sales",
            "unit_label": "KRW",
            "raw_value_history": json.dumps({"2026-05": 100.0}),
        }

    monkeypatch.setattr("pipeline.scripts.api.dynamic_market.aggregator.db.fetch_all", fake_fetch_all)
    monkeypatch.setattr("pipeline.scripts.api.dynamic_market.aggregator.db.iter_rows", fake_iter_rows)

    metrics = MetricAggregator(mart_db="jw_mart").aggregate(
        brands=(BrandRef("a", "A", "C10A1"),),
        source="ubist",
        measure="sales",
        period_range=PeriodRange(),
        top_n=20,
    )

    metric_sql = calls[0]
    assert "raw_value_history" in metric_sql
    assert "by_dimension" not in metric_sql
    assert "dimension_data" not in metric_sql
    assert "dimension_channel_data" not in metric_sql
    assert "channel_data" not in metric_sql
    assert "channel_specialty_matrix" not in metric_sql
    assert "channel_specialty_matrix" not in metric_sql
    assert metrics.all_brands[0].channel_specialty_matrix == {}
    assert metrics.all_brands[0].analysis_row["by_dimension"] is None
    assert metrics.ubist_specialty_channels == ("전체", "종합병원 순환기", "의원 IGF")
    assert metrics.ubist_specialty_target_channels == (
        {
            "code": "GH Cardio",
            "display_name": "종합병원 순환기",
            "facility_abbr": "GH",
            "facility_kor": "종합병원",
            "facility_raw_values": ["상급종합병원", "종합병원", "병원"],
            "specialty_abbr": "Cardio",
            "specialty_kor": "순환기",
            "specialty_raw_values": ["순환기(Cardiology IM)"],
        },
        {
            "code": "CL IGF",
            "display_name": "의원 IGF",
            "facility_abbr": "CL",
            "facility_kor": "의원",
            "facility_raw_values": ["의원"],
            "specialty_abbr": "IGF",
            "specialty_kor": "IGF",
            "specialty_raw_values": [
                "가정의학과(FM)",
                "일반의(GP)",
                "알레르기(Allergy IM)",
                "내분비(Endocrinology IM)",
                "순환기(Cardiology IM)",
                "신장(Nephrology IM)",
                "류마티스(Rheumatology IM)",
                "소화기(Gastroenterology IM)",
                "감염(Infection Disease IM)",
                "혈액종양(Hemoto Oncology IM)",
                "호흡기(Pulmonology IM)",
                "분리되지 않은 내과",
            ],
        },
    )


def test_general_metric_rows_use_superset_scope_with_pair_filter(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def fake_iter_rows(sql: str, params: tuple[object, ...]):
        seen["sql"] = sql
        seen["params"] = params
        yield {
            "brand_key": "a",
            "brand_name": "A",
            "atc4_code": "C10A1",
            "source": "ubist",
            "measure": "sales",
            "unit_label": "KRW",
            "raw_value_history": "{}",
        }
        yield {
            "brand_key": "a",
            "brand_name": "A",
            "atc4_code": "C10C0",
            "source": "ubist",
            "measure": "sales",
            "unit_label": "KRW",
            "raw_value_history": "{}",
        }
        yield {
            "brand_key": "b",
            "brand_name": "B",
            "atc4_code": "C10C0",
            "source": "ubist",
            "measure": "sales",
            "unit_label": "KRW",
            "raw_value_history": "{}",
        }

    monkeypatch.setattr("pipeline.scripts.api.dynamic_market.aggregator.db.iter_rows", fake_iter_rows)
    monkeypatch.setattr("pipeline.scripts.api.dynamic_market.aggregator.db.fetch_all", lambda *_: [])

    rows = list(
        MetricAggregator(mart_db="jw_mart")._iter_metric_rows(
            brands=(BrandRef("a", "A", "C10A1"), BrandRef("b", "B", "C10C0")),
            source="ubist",
            measure="sales",
            channel_axis=None,
        )
    )

    assert "brand_key IN" in str(seen["sql"])
    assert "atc4_code IN" in str(seen["sql"])
    assert "(brand_key, atc4_code) IN" not in str(seen["sql"])
    assert [(row["brand_key"], row["atc4_code"]) for row in rows] == [("a", "C10A1"), ("b", "C10C0")]


def test_history_prefers_cached_history_by_period() -> None:
    brand = BrandMetric(
        "a",
        "A",
        "C10A1",
        1.0,
        100.0,
        1,
        "2026-02",
        2.0,
        monthly_series=({"period": "2026-02", "value": 2.0},),
        history_by_period={"2026-01": 1.0, "2026-02": 2.0},
    )

    assert cause_time.history(brand) == {"2026-01": 1.0, "2026-02": 2.0}


def test_display_matrix_rows_pins_focus_then_top_competitors_without_mutating_values() -> None:
    focus = BrandMetric("focus", "Focus", "C10A1", 10.0, 1.0, 8, "2026-05", 10.0)
    rows = [
        {
            "brand": f"Brand {index}",
            "brand_key": f"b{index}",
            "value_recent": float(100 - index),
            "share_pct": float(index),
            "is_others": False,
        }
        for index in range(1, 8)
    ]
    focus_row = {
        "brand": "Focus",
        "brand_key": "focus",
        "value_recent": 10.0,
        "share_pct": 99.0,
        "is_others": False,
    }
    rows.append(focus_row)

    selected = display_matrix_rows(rows, focus=focus)

    assert [row["brand_key"] for row in selected] == ["focus", "b1", "b2", "b3", "b4", "b5"]
    assert selected[0] is focus_row
    assert selected[0]["share_pct"] == 99.0
    assert all(not row.get("is_others") for row in selected)


def test_build_cause_data_cuts_matrix_cards_but_keeps_full_matrix_for_kpi() -> None:
    brands = tuple(
        BrandMetric(
            f"b{index}",
            f"Brand {index}",
            "C10A1",
            float(100 - index),
            0.0,
            index,
            "2026-05",
            float(100 - index),
            monthly_series=(
                {"period": "2026-04", "value": float(80 - index)},
                {"period": "2026-05", "value": float(100 - index)},
            ),
        )
        for index in range(1, 8)
    )
    focus = BrandMetric(
        "focus",
        "Focus",
        "C10A1",
        10.0,
        0.0,
        8,
        "2026-05",
        10.0,
        monthly_series=(
            {"period": "2026-04", "value": 8.0},
            {"period": "2026-05", "value": 10.0},
        ),
    )
    metrics = AggregatedMetrics(
        source="ubist",
        measure="sales",
        unit_label="KRW",
        market_size=0.0,
        hhi=None,
        cagr=10.0,
        monthly_series=(
            {"period": "2026-04", "market_size": 512.0},
            {"period": "2026-05", "market_size": 689.0},
        ),
        brands=brands,
        all_brands=brands + (focus,),
    )
    data = cause_payload.build_cause_data(
        definition=MarketDefinition(view="general", filter_echo={}, source="ubist", measure="sales"),
        metrics=metrics,
        focus=focus,
    )

    assert [row["brand_key"] for row in data["ei_ms_matrix"]["data"]] == ["focus", "b1", "b2", "b3", "b4", "b5"]
    assert [row["brand_key"] for row in data["growth_contribution_ms_matrix"]["data"]] == [
        "focus",
        "b1",
        "b2",
        "b3",
        "b4",
        "b5",
    ]
    assert data["ei_ms_matrix"]["data"][0]["value_recent"] == 10.0
    assert data["kpi"]["target_brand"] == "Focus"
    assert data["kpi"]["brand_value_recent"] == 10.0


def test_build_cause_data_fills_analysis_levels_from_focus_ml_market(monkeypatch) -> None:
    analysis_row = {
        "source": "ubist",
        "measure": "sales",
        "unit_label": "KRW",
        "dimension_data": json.dumps({}),
        "dimension_channel_data": json.dumps({}),
        "channel_data": json.dumps({"의원": {"2026-04": {"raw_value": 80.0}, "2026-05": {"raw_value": 100.0}}}),
        "overlay_data": json.dumps({}),
    }
    strategic_rows = [
        {
            "brand_key": "focus",
            "brand_name": "Focus",
            "by_dimension": json.dumps({"class": "DPP4", "molecule": "Sitagliptin"}),
            "is_jw": 1,
        }
    ]
    calls: list[tuple[str, tuple[object, ...]]] = []

    def fake_fetch_all(sql: str, params: tuple[object, ...]) -> list[dict[str, object]]:
        calls.append((sql, params))
        if "mart_general_brand_metric" in sql:
            return [
                {
                    "brand_key": "focus",
                    "brand_name": "Focus",
                    "atc4_code": "A10N1",
                    **analysis_row,
                }
            ]
        if "mart_general_filter_dimension_metric" in sql:
            return []
        return [dict(row) for row in strategic_rows]

    monkeypatch.setattr("pipeline.scripts.api.dynamic_market.analysis_level_dimensions.db.fetch_all", fake_fetch_all)
    metrics = AggregatedMetrics(
        source="ubist",
        measure="sales",
        unit_label="KRW",
        market_size=100.0,
        hhi=None,
        cagr=10.0,
        monthly_series=(
            {"period": "2026-04", "market_size": 80.0},
            {"period": "2026-05", "market_size": 100.0},
        ),
        brands=(
            BrandMetric(
                "focus",
                "Focus",
                "A10N1",
                100.0,
                100.0,
                1,
                "2026-05",
                100.0,
                monthly_series=({"period": "2026-05", "value": 100.0},),
                history_by_period={"2026-04": 80.0, "2026-05": 100.0},
                analysis_row=analysis_row,
            ),
        ),
        all_brands=(
            BrandMetric(
                "focus",
                "Focus",
                "A10N1",
                100.0,
                100.0,
                1,
                "2026-05",
                100.0,
                monthly_series=({"period": "2026-05", "value": 100.0},),
                history_by_period={"2026-04": 80.0, "2026-05": 100.0},
                analysis_row=analysis_row,
            ),
        ),
    )

    data = cause_payload.build_cause_data(
        definition=MarketDefinition(
            view="general",
            filter_echo={},
            source="ubist",
            measure="sales",
            focus_brand_key="focus",
            market_catalog_row={"ml_id": "ml_999", "analyze_class": True, "analyze_molecule": True},
        ),
        metrics=metrics,
        focus=metrics.all_brands[0],
    )

    assert any(call[1] == ("ml_999", "ubist", "sales") for call in calls)
    class_segments = data["analysis_levels"]["data"]["Class"]["by_channel"]["전체"]
    molecule_segments = data["analysis_levels"]["data"]["Molecule"]["by_channel"]["전체"]
    assert [item["name"] for item in class_segments] == ["전체", "DPP4"]
    assert [item["name"] for item in molecule_segments] == ["전체", "Sitagliptin"]
    assert data["level_top5_trend"]["by_level"]["Class"]["values"][1]["value"] == "DPP4"


def test_ubist_channel_summary_uses_superset_scope_with_pair_filter(monkeypatch, caplog) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []

    def fake_fetch_all(sql: str, params: tuple[object, ...]) -> list[dict[str, object]]:
        calls.append((sql, params))
        return []

    def fake_iter_rows(sql: str, params: tuple[object, ...]):
        calls.append((sql, params))
        if "channel_specialty_matrix" in sql and "raw_value_history" not in sql:
            assert "(brand_key, atc4_code) IN" not in sql
            assert "brand_key IN" in sql
            assert "atc4_code IN" in sql
            yield {
                "brand_key": "a",
                "atc4_code": "C10A1",
                "channel_specialty_matrix": json.dumps(
                    {"종합병원": {"순환기(Cardiology IM)": {"2026-05": 90.0}}},
                    ensure_ascii=False,
                ),
            }
            yield {
                "brand_key": "a",
                "atc4_code": "C10C0",
                "channel_specialty_matrix": json.dumps(
                    {"의원": {"가정의학과(FM)": {"2026-05": 999.0}}},
                    ensure_ascii=False,
                ),
            }
            return
        yield {
            "brand_key": "a",
            "brand_name": "A",
            "atc4_code": "C10A1",
            "source": "ubist",
            "measure": "sales",
            "unit_label": "KRW",
            "raw_value_history": json.dumps({"2026-05": 100.0}),
        }

    monkeypatch.setattr("pipeline.scripts.api.dynamic_market.aggregator.db.fetch_all", fake_fetch_all)
    monkeypatch.setattr("pipeline.scripts.api.dynamic_market.aggregator.db.iter_rows", fake_iter_rows)

    with caplog.at_level("DEBUG", logger="pipeline.scripts.api.dynamic_market.aggregator"):
        metrics = MetricAggregator(mart_db="jw_mart").aggregate(
            brands=(BrandRef("a", "A", "C10A1"),),
            source="ubist",
            measure="sales",
            period_range=PeriodRange(),
            top_n=20,
        )

    summary_sql, summary_params = next(
        (sql, params)
        for sql, params in calls
        if "channel_specialty_matrix" in sql and "raw_value_history" not in sql
    )
    assert summary_params == ("ubist", "sales", "a", "C10A1")
    assert metrics.ubist_specialty_channels == ("전체", "종합병원 순환기")
    assert "filtered_rows=1" in caplog.text


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


def test_collect_ubist_channel_latest_totals_reads_only_latest_period_without_double_counting() -> None:
    matrix = {
        "종합병원": {
            "순환기(Cardiology IM)": {"2026-04": 30.0, "2026-05": 40.0},
            "내분비(Endocrinology IM)": {"2026-05": 70.0},
            "내과(IM)": {"2026-05": 999.0},
        },
        "의원": {
            "가정의학과(FM)": {"2026-05": 10.0},
            "일반의(GP)": {"2026-05": 20.0},
            "분리되지 않은 내과": {"2026-05": 30.0},
        },
    }

    totals: dict[str, float] = {}
    collect_ubist_channel_latest_totals(json.dumps(matrix, ensure_ascii=True), "2026-05", totals)

    assert totals == {
        "GH Cardio": 40.0,
        "GH Endo": 70.0,
        "CL IGF": 60.0,
    }


def test_collect_ubist_channel_latest_totals_accepts_quoted_numbers() -> None:
    matrix = {
        "종합병원": {
            "순환기(Cardiology IM)": {"2026-04": "30.0", "2026-05": "40.0"},
            "내분비(Endocrinology IM)": {"2026-05": "70.0"},
        },
        "의원": {
            "가정의학과(FM)": {"2026-05": "10.0"},
            "일반의(GP)": {"2026-05": "20.0"},
        },
    }
    raw = json.dumps(matrix, ensure_ascii=True)

    totals: dict[str, float] = {}
    collect_ubist_channel_latest_totals(raw, "2026-05", totals)

    assert totals == {
        "GH Cardio": 40.0,
        "GH Endo": 70.0,
        "CL IGF": 30.0,
    }


def test_collect_ubist_channel_latest_totals_reuses_pair_mapping_cache(monkeypatch) -> None:
    raw = json.dumps(
        {
            "종합병원": {
                "순환기(Cardiology IM)": {"2026-05": 40.0},
            },
        },
        ensure_ascii=True,
    )
    calls: list[tuple[str, str]] = []

    def fake_raw_pair_to_channel_code(facility: str, specialty: str) -> str | None:
        calls.append((facility, specialty))
        return "GH Cardio"

    monkeypatch.setattr(aggregator_module, "raw_pair_to_channel_code", fake_raw_pair_to_channel_code)

    totals: dict[str, float] = {}
    cache: dict[tuple[str, str], str | None] = {}
    collect_ubist_channel_latest_totals(raw, "2026-05", totals, channel_code_cache=cache)
    collect_ubist_channel_latest_totals(raw, "2026-05", totals, channel_code_cache=cache)

    assert totals == {"GH Cardio": 80.0}
    assert calls == [("종합병원", "순환기(Cardiology IM)")]


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


def test_cause_payload_fills_analysis_level_sections_from_focus_catalog(monkeypatch) -> None:
    monkeypatch.setattr("pipeline.scripts.api.dynamic_market.analysis_level_dimensions.db.fetch_all", lambda *_args: [])

    definition = MarketDefinition(
        view="general",
        filter_echo={"view": "general", "atc4": ["A10N1"], "source": "ubist", "measure": "sales"},
        source="ubist",
        measure="sales",
        focus_brand_key="focus",
        market_catalog_row={
            "ml_id": "ml_003",
            "analyze_class": 1,
            "analyze_molecule": 1,
            "analyze_dosage_form": 0,
            "analyze_strength_pack": 0,
            "analyze_nhi_type": 0,
            "analyze_ox_gx": 0,
        },
    )
    periods = ("2026-01", "2026-02")
    class_series = {
        period: {"raw_value": value}
        for period, value in zip(periods, (100.0, 120.0))
    }
    class_channel_series = {"종합병원": class_series, "의원": {"2026-01": {"raw_value": 10.0}, "2026-02": {"raw_value": 20.0}}}
    focus = BrandMetric(
        "focus",
        "Focus Brand",
        "A10N1",
        220.0,
        55.0,
        1,
        "2026-02",
        120.0,
        ({"period": "2026-01", "value": 100.0}, {"period": "2026-02", "value": 120.0}),
        history_by_period={"2026-01": 100.0, "2026-02": 120.0},
        analysis_row={
            "by_dimension": json.dumps({"class": "DPP4", "molecule": "Anagliptin", "company": "JW"}, ensure_ascii=False),
            "dimension_data": json.dumps({"class": {"DPP4": class_series}, "molecule": {"Anagliptin": class_series}}, ensure_ascii=False),
            "dimension_channel_data": json.dumps({"class": {"DPP4": class_channel_series}}, ensure_ascii=False),
            "channel_data": json.dumps(class_channel_series, ensure_ascii=False),
        },
    )
    competitor = BrandMetric(
        "comp",
        "Competitor",
        "A10N1",
        180.0,
        45.0,
        2,
        "2026-02",
        80.0,
        ({"period": "2026-01", "value": 100.0}, {"period": "2026-02", "value": 80.0}),
        history_by_period={"2026-01": 100.0, "2026-02": 80.0},
        analysis_row={
            "by_dimension": json.dumps({"class": "DPP4", "molecule": "Other", "company": "Other Co"}, ensure_ascii=False),
            "dimension_data": json.dumps({"class": {"DPP4": {"2026-01": {"raw_value": 100.0}, "2026-02": {"raw_value": 80.0}}}}, ensure_ascii=False),
            "dimension_channel_data": json.dumps({}, ensure_ascii=False),
            "channel_data": json.dumps({"종합병원": {"2026-01": {"raw_value": 50.0}, "2026-02": {"raw_value": 40.0}}}, ensure_ascii=False),
        },
    )
    metrics = AggregatedMetrics(
        source="ubist",
        measure="sales",
        unit_label="KRW",
        market_size=400.0,
        hhi=None,
        cagr=None,
        monthly_series=({"period": "2026-01", "market_size": 200.0}, {"period": "2026-02", "market_size": 200.0}),
        brands=(focus, competitor),
        all_brands=(focus, competitor),
    )

    payload = build_cause_payload(definition=definition, metrics=metrics)

    analysis_levels = payload["data"]["analysis_levels"]
    assert analysis_levels["channels"] == ["전체", "상급종병", "종병", "병원", "의원", "보건소", "기타"]
    assert analysis_levels["levels"][:3] == ["Class", "Molecule", "Brand"]
    assert any(
        segment["name"] == "DPP4"
        for segment in analysis_levels["data"]["Class"]["by_channel"]["전체"]
    )
    assert analysis_levels["data"]["Class"]["by_channel"]["종병"]
    assert "종합병원 순환기" not in analysis_levels["channels"]
    assert payload["data"]["analysis_level_market_status"]["channels"] == analysis_levels["channels"]
    assert payload["data"]["level_top5_trend"]["by_level"]["Class"]["values"]


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


def test_unfiltered_general_aggregation_keeps_metric_fetch_slim(monkeypatch) -> None:
    calls: list[str] = []

    def fake_iter_rows(sql: str, params: tuple[str, ...]):
        calls.append(sql)
        yield {
            "brand_key": "brand-a",
            "brand_name": "Brand A",
            "atc4_code": "A10A1",
            "source": "ubist",
            "measure": "sales",
            "unit_label": "KRW",
            "raw_value_history": json.dumps({"2026-01": 100, "2026-02": 120}),
        }

    def fake_fetch_all(sql: str, params: tuple[str, ...]) -> list[dict]:
        calls.append(sql)
        return []

    monkeypatch.setattr("pipeline.scripts.api.dynamic_market.aggregator.db.iter_rows", fake_iter_rows)
    monkeypatch.setattr("pipeline.scripts.api.dynamic_market.aggregator.db.fetch_all", fake_fetch_all)

    metrics = MetricAggregator(mart_db="jw_mart").aggregate(
        brands=(BrandRef("brand-a", "Brand A", "A10A1"),),
        source="ubist",
        measure="sales",
        period_range=PeriodRange(),
        top_n=20,
    )

    analysis_row = metrics.all_brands[0].analysis_row
    assert metrics.market_size == 220.0
    assert analysis_row["dimension_data"] is None
    assert not any("mart_general_filter_dimension_metric" in sql for sql in calls)


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


@pytest.mark.parametrize("incoming_ml_id", ["strategy_006", "ml_003"])
def test_dynamic_route_prefers_brand_catalog_market_over_incoming_ml_id(
    monkeypatch: pytest.MonkeyPatch,
    incoming_ml_id: str,
) -> None:
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
                "ml_id": incoming_ml_id,
                "focus_brand_key": "리바로",
            },
            "source": "ubist",
            "measure": "sales",
        }
    )

    definition = dynamic_market_route._resolve_definition(payload)

    assert captured["ml_id"] == "ml_006"
    assert definition.strategic_market_id == "ml_006"


def test_strategic_resolver_uses_cd_table_for_competitive_dynamics(monkeypatch) -> None:
    calls: list[str] = []

    def fake_fetch_all(sql: str, params: tuple[str, ...]) -> list[dict]:
        calls.append(sql)
        assert params[:3] == ("cd_002", "iqvia_nsa", "sales")
        return [{"brand_key": "brand-cd", "brand_name": "CD Brand", "atc4_code": ""}]

    monkeypatch.setattr("pipeline.scripts.api.dynamic_market.resolvers.db.fetch_all", fake_fetch_all)
    monkeypatch.setattr("pipeline.scripts.api.dynamic_market.resolvers.db.fetch_one", lambda *_args, **_kwargs: {"cd_id": "cd_002"})
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
