from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from pydantic import ValidationError


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.scripts.api.dynamic_market import aggregator as aggregator_module
from pipeline.scripts.api.dynamic_market import cause_payload, cause_time, resolvers, strategic_runtime
from pipeline.scripts.api.dynamic_market import general_analysis_levels
from pipeline.scripts.api.composers import cache_to_response
from pipeline.scripts.etl import ubist_channel_resolver
from pipeline.scripts.api.dynamic_market.aggregator import (
    MetricAggregator,
    collect_ubist_channel_latest_totals,
    compute_cagr,
    compute_hhi,
    month_distance,
)
from pipeline.scripts.api.dynamic_market.aggregator import sidecar_rows_to_metric_rows
from pipeline.scripts.api.dynamic_market.composer import ResponseComposer
from pipeline.scripts.api.composers.cache_to_response import compose_cached_json
from pipeline.scripts.api.composers.number_format import deep_format_numbers
from pipeline.scripts.api.dynamic_market.cause_sections import (
    display_matrix_rows,
    growth_contribution,
    matrix_rows,
)
from pipeline.scripts.api.dynamic_market.cause_payload import build_cause_payload
from pipeline.scripts.api.dynamic_market.resolvers import GeneralViewResolver, StrategicViewResolver
from pipeline.scripts.api.dynamic_market.types import (
    AggregatedMetrics,
    BrandMetric,
    BrandRef,
    DimensionFilter,
    DynamicMarketInputError,
    DynamicMarketPeriodNoDataError,
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


def test_month_distance_accepts_month_and_quarter_periods() -> None:
    assert month_distance("2024-01", "2024-12") == 11
    assert month_distance("2025-Q1", "2026-Q2") == 15


def test_general_growth_contribution_uses_distinct_source_period_windows() -> None:
    periods = tuple(
        f"{year}-{month:02d}"
        for year in range(2021, 2027)
        for month in range(1, 13)
    )[:65]
    brands = (
        BrandMetric(
            "focus",
            "Focus",
            "A10N1",
            0.0,
            0.0,
            1,
            periods[-1],
            0.0,
            tuple(
                {"period": period, "value": float(index)}
                for index, period in enumerate(periods, start=1)
            ),
        ),
        BrandMetric(
            "other",
            "Other",
            "A10N1",
            0.0,
            0.0,
            2,
            periods[-1],
            0.0,
            tuple(
                {"period": period, "value": float(index * index)}
                for index, period in enumerate(periods, start=1)
            ),
        ),
    )

    payload = growth_contribution(brands, focus=brands[0], source="ubist")

    assert [payload["windows"][f"{years}y"]["period_start"] for years in range(1, 6)] == [
        periods[-12],
        periods[-24],
        periods[-36],
        periods[-48],
        periods[-60],
    ]
    assert len(
        {
            payload["windows"][f"{years}y"]["by_brand"]["top_contributors"][0]["contribution_value"]
            for years in range(1, 6)
        }
    ) == 5


def test_general_growth_contribution_marks_truncated_history_without_zero_fallback() -> None:
    periods = ("2025-01", "2025-02", "2025-03")
    brand = BrandMetric(
        "focus",
        "Focus",
        "A10N1",
        60.0,
        100.0,
        1,
        periods[-1],
        30.0,
        tuple(
            {"period": period, "value": float(index * 10)}
            for index, period in enumerate(periods, start=1)
        ),
    )

    payload = growth_contribution((brand,), focus=brand, source="ubist")

    for window in payload["windows"].values():
        assert window["period_start"] == "2025-01"
        assert window["period_start_actual"] == "2025-01"
        assert window["reason"] == "earliest_available"
        assert window["market_start"] == 10.0


def test_general_growth_contribution_uses_quarterly_iqvia_stride() -> None:
    periods = tuple(
        f"{year}-Q{quarter}"
        for year in range(2021, 2027)
        for quarter in range(1, 5)
    )[:21]
    brand = BrandMetric(
        "focus",
        "Focus",
        "A10N1",
        0.0,
        100.0,
        1,
        periods[-1],
        21.0,
        tuple({"period": period, "value": float(index)} for index, period in enumerate(periods, start=1)),
    )

    payload = growth_contribution((brand,), focus=brand, source="iqvia_nsa")

    assert [payload["windows"][f"{years}y"]["period_start"] for years in range(1, 6)] == [
        periods[-4],
        periods[-8],
        periods[-12],
        periods[-16],
        periods[-20],
    ]


def test_cause_time_cagr_helpers_accept_iqvia_quarter_periods() -> None:
    history = {"2025-Q1": 100.0, "2026-Q2": 133.1}

    assert cause_time.brand_cagr(history) == pytest.approx(25.70207430874425)
    assert cause_time.period_years(history) == 1.25


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


def _aggregate_rank_fixture(
    monkeypatch,
    *,
    source: str,
    histories: dict[str, dict[str, float]],
    period_range: PeriodRange = PeriodRange(),
) -> AggregatedMetrics:
    def fake_iter_rows(sql: str, _params: tuple[object, ...]):
        if "raw_value_history" not in sql:
            return
        for brand_key, history in histories.items():
            yield {
                "brand_key": brand_key,
                "brand_name": brand_key.upper(),
                "atc4_code": "C10A1",
                "source": source,
                "measure": "sales",
                "unit_label": "KRW",
                "raw_value_history": json.dumps(history),
            }

    monkeypatch.setattr("pipeline.scripts.api.dynamic_market.aggregator.db.fetch_all", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("pipeline.scripts.api.dynamic_market.aggregator.db.iter_rows", fake_iter_rows)
    return MetricAggregator(mart_db="jw_mart").aggregate(
        brands=tuple(BrandRef(key, key.upper(), "C10A1") for key in histories),
        source=source,
        measure="sales",
        period_range=period_range,
        top_n=100,
    )


def test_general_ubist_default_window_hides_cagr_baseline_month(monkeypatch) -> None:
    # Given 61 monthly points whose oldest point is required for exact five-year CAGR.
    periods = tuple(
        f"{year}-{month:02d}"
        for year in range(2021, 2027)
        for month in range(1, 13)
    )[5:66]
    history = {
        period: float(value)
        for period, value in zip(periods, range(100, 161), strict=True)
    }

    # When the unbounded UBIST market response is aggregated.
    metrics = _aggregate_rank_fixture(
        monkeypatch,
        source="ubist",
        histories={"focus": history},
    )

    # Then the calculation baseline stays hidden while CAGR uses the full 60-month interval.
    assert [point["period"] for point in metrics.monthly_series] == list(periods[-60:])
    assert [point["period"] for point in metrics.all_brands[0].monthly_series] == list(periods[-60:])
    assert metrics.cagr == round(((160.0 / 100.0) ** (1 / 5) - 1) * 100, 6)


def test_general_ubist_rank_uses_latest_value_with_total_value_tiebreak(monkeypatch) -> None:
    # Given cumulative leaders that tie or have no value in the market's common latest period.
    histories = {
        "positive-rich-old": {"2025-04": 100.0, "2026-05": 10.0},
        "positive-new": {"2026-05": 10.0},
        "zero-rich": {"2025-04": 90.0},
        "zero-less": {"2025-03": 50.0},
    }

    # When general UBIST metrics are ranked.
    metrics = _aggregate_rank_fixture(monkeypatch, source="ubist", histories=histories)

    # Then latest-positive brands lead with cumulative-sales tiebreaks, followed by zero-value brands in cumulative order.
    assert [(brand.brand_key, brand.rank) for brand in metrics.all_brands] == [
        ("positive-rich-old", 1),
        ("positive-new", 2),
        ("zero-rich", 3),
        ("zero-less", 4),
    ]
    assert all(isinstance(brand.rank, int) and brand.rank > 0 for brand in metrics.all_brands)


def test_general_iqvia_rank_uses_source_latest_quarter_outside_response_period_range(monkeypatch) -> None:
    # Given an old cumulative leader and a low-value brand in the source's latest quarter.
    histories = {
        "old-leader": {"2025-Q2": 100.0},
        "current": {"2026-Q2": 1.0},
    }

    # When IQVIA metrics are ranked while the response period range selects only the old year.
    metrics = _aggregate_rank_fixture(
        monkeypatch,
        source="iqvia_nsa",
        histories=histories,
        period_range=PeriodRange("2025-Q1", "2025-Q4"),
    )

    # Then the source's common latest quarter, not the response range or cumulative total, determines rank.
    assert [(brand.brand_key, brand.rank) for brand in metrics.all_brands] == [("current", 1), ("old-leader", 2)]


def test_aggregate_selects_focus_plus_top_competitors_by_total_value(monkeypatch) -> None:
    def fake_iter_rows(sql: str, _params: tuple[object, ...]):
        if "raw_value_history" not in sql:
            return
        for brand_key, values in {
            "focus": {"2026-01": 1.0, "2026-02": 1.0},
            "a": {"2026-01": 100.0, "2026-02": 0.0},
            "b": {"2026-01": 1.0, "2026-02": 200.0},
            "c": {"2026-01": 90.0, "2026-02": 90.0},
            "d": {"2026-01": 80.0, "2026-02": 80.0},
            "e": {"2026-01": 70.0, "2026-02": 70.0},
            "f": {"2026-01": 60.0, "2026-02": 60.0},
            "g": {"2026-01": 50.0, "2026-02": 50.0},
        }.items():
            yield {
                "brand_key": brand_key,
                "brand_name": brand_key.upper(),
                "atc4_code": "C10A1",
                "source": "ubist",
                "measure": "sales",
                "unit_label": "KRW",
                "raw_value_history": json.dumps(values),
            }

    monkeypatch.setattr("pipeline.scripts.api.dynamic_market.aggregator.db.fetch_all", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("pipeline.scripts.api.dynamic_market.aggregator.db.iter_rows", fake_iter_rows)

    metrics = MetricAggregator(mart_db="jw_mart").aggregate(
        brands=(
            BrandRef("focus", "FOCUS", "C10A1"),
            BrandRef("a", "A", "C10A1"),
            BrandRef("b", "B", "C10A1"),
            BrandRef("c", "C", "C10A1"),
            BrandRef("d", "D", "C10A1"),
            BrandRef("e", "E", "C10A1"),
            BrandRef("f", "F", "C10A1"),
            BrandRef("g", "G", "C10A1"),
        ),
        source="ubist",
        measure="sales",
        period_range=PeriodRange(),
        top_n=20,
        selected_brand_key="focus",
    )

    assert [item.brand_key for item in metrics.brands] == ["focus", "b", "c", "d", "e", "f"]


def test_general_aggregate_keeps_ubist_matrix_columns_for_specialty_channels(monkeypatch) -> None:
    calls: list[str] = []

    def fake_fetch_all(sql: str, params: tuple[object, ...]) -> list[dict[str, object]]:
        calls.append(sql)
        assert "ubist_channel_by_display" not in sql
        return []

    def fake_iter_rows(sql: str, params: tuple[object, ...]):
        calls.append(sql)
        assert "ubist_channel_by_display" not in sql
        assert "channel_specialty_matrix" in sql
        assert "audit_code_matrix" in sql
        yield {
            "brand_key": "a",
            "brand_name": "A",
            "atc4_code": "C10A1",
            "source": "ubist",
            "measure": "sales",
            "unit_label": "KRW",
            "raw_value_history": json.dumps({"2026-05": 100.0}),
            "channel_specialty_matrix": json.dumps(
                {
                    "종합병원": {"순환기(Cardiology IM)": {"2026-05": 90.0}},
                    "의원": {"가정의학과(FM)": {"2026-05": 10.0}},
                },
                ensure_ascii=False,
            ),
            "audit_code_matrix": json.dumps({}),
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
    assert "by_dimension" in metric_sql
    assert "dimension_data" not in metric_sql
    assert "dimension_channel_data" not in metric_sql
    assert "channel_data" not in metric_sql
    assert "channel_specialty_matrix" in metric_sql
    assert "audit_code_matrix" in metric_sql
    assert len(calls) == 1
    assert metrics.all_brands[0].channel_specialty_matrix
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


def test_display_matrix_rows_uses_total_sales_before_recent_value() -> None:
    focus = BrandMetric("focus", "Focus", "C10A1", 1.0, 1.0, 8, "2026-05", 1.0)
    rows = [
        {"brand": "Focus", "brand_key": "focus", "total_value": 1.0, "value_recent": 1.0},
        {"brand": "A", "brand_key": "a", "total_value": 10.0, "value_recent": 100.0},
        {"brand": "B", "brand_key": "b", "total_value": 50.0, "value_recent": 1.0},
        {"brand": "C", "brand_key": "c", "total_value": 50.0, "value_recent": 2.0},
    ]

    selected = display_matrix_rows(rows, focus=focus)

    assert [row["brand_key"] for row in selected] == ["focus", "b", "c", "a"]


def test_matrix_rows_populates_growth_contribution_percent_for_chart_points() -> None:
    metrics = AggregatedMetrics(
        source="ubist",
        measure="sales",
        unit_label="KRW",
        market_size=160.0,
        hhi=None,
        cagr=10.0,
        monthly_series=(
            {"period": "2026-04", "market_size": 100.0},
            {"period": "2026-05", "market_size": 160.0},
        ),
        brands=(),
        all_brands=(
            BrandMetric(
                "a",
                "A",
                "C10A1",
                40.0,
                25.0,
                1,
                "2026-05",
                40.0,
                history_by_period={"2026-04": 10.0, "2026-05": 40.0},
            ),
            BrandMetric(
                "b",
                "B",
                "C10A1",
                120.0,
                75.0,
                2,
                "2026-05",
                120.0,
                history_by_period={"2026-04": 90.0, "2026-05": 120.0},
            ),
        ),
    )

    rows = matrix_rows(metrics=metrics, focus=metrics.all_brands[0])

    assert rows[0]["growth_contribution"] == 30.0
    assert rows[0]["contribution"] == 30.0
    assert rows[0]["growth_contribution_pct"] == pytest.approx(50.0)
    assert rows[0]["contribution_pct"] == pytest.approx(50.0)
    assert rows[1]["growth_contribution_pct"] == pytest.approx(50.0)
    assert rows[1]["contribution_pct"] == pytest.approx(50.0)


def test_matrix_rows_uses_recent_four_market_share_points_for_momentum() -> None:
    metrics = AggregatedMetrics(
        source="ubist",
        measure="sales",
        unit_label="KRW",
        market_size=100.0,
        hhi=None,
        cagr=10.0,
        monthly_series=tuple(
            {"period": f"2026-0{index}", "market_size": 100.0}
            for index in range(1, 5)
        ),
        brands=(),
        all_brands=(
            BrandMetric(
                "focus",
                "Focus",
                "C10A1",
                100.0,
                40.0,
                1,
                "2026-04",
                40.0,
                history_by_period={
                    "2026-01": 10.0,
                    "2026-02": 20.0,
                    "2026-03": 30.0,
                    "2026-04": 40.0,
                },
            ),
        ),
    )

    rows = matrix_rows(metrics=metrics, focus=metrics.all_brands[0])

    assert rows[0]["momentum_score"] == pytest.approx(10.0)


def test_matrix_rows_keeps_zero_contribution_instead_of_momentum() -> None:
    metrics = AggregatedMetrics(
        source="ubist",
        measure="sales",
        unit_label="KRW",
        market_size=100.0,
        hhi=None,
        cagr=10.0,
        monthly_series=tuple(
            {"period": f"2026-0{index}", "market_size": 100.0}
            for index in range(1, 5)
        ),
        brands=(),
        all_brands=(
            BrandMetric(
                "zero",
                "Zero contribution",
                "C10A1",
                70.0,
                10.0,
                1,
                "2026-04",
                10.0,
                history_by_period={
                    "2026-01": 10.0,
                    "2026-02": 20.0,
                    "2026-03": 30.0,
                    "2026-04": 10.0,
                },
            ),
        ),
    )

    row = matrix_rows(metrics=metrics, focus=metrics.all_brands[0])[0]

    assert row["momentum_score"] != 0.0
    assert row["growth_contribution"] == 0.0
    assert row["contribution_pct"] is None


def test_matrix_rows_returns_none_for_momentum_with_fewer_than_four_share_points() -> None:
    metrics = AggregatedMetrics(
        source="ubist",
        measure="sales",
        unit_label="KRW",
        market_size=100.0,
        hhi=None,
        cagr=10.0,
        monthly_series=(
            {"period": "2026-01", "market_size": 100.0},
            {"period": "2026-02", "market_size": 100.0},
            {"period": "2026-03", "market_size": 100.0},
        ),
        brands=(),
        all_brands=(
            BrandMetric(
                "focus",
                "Focus",
                "C10A1",
                60.0,
                30.0,
                1,
                "2026-03",
                30.0,
                history_by_period={"2026-01": 10.0, "2026-02": 20.0, "2026-03": 30.0},
            ),
        ),
    )

    rows = matrix_rows(metrics=metrics, focus=metrics.all_brands[0])

    assert rows[0]["momentum_score"] is None


def test_build_cause_data_cuts_matrix_cards_but_keeps_full_matrix_for_kpi() -> None:
    # Given a focus brand below the portal's top-100 matrix cutoff.
    brands = tuple(
        BrandMetric(
            f"b{index}",
            f"Brand {index}",
            "C10A1",
            float(1_000 - index),
            0.0,
            index,
            "2026-05",
            float(1_000 - index),
            monthly_series=(
                {"period": "2026-04", "value": float(900 - index)},
                {"period": "2026-05", "value": float(1_000 - index)},
            ),
        )
        for index in range(1, 106)
    )
    focus = BrandMetric(
        "focus",
        "Focus",
        "C10A1",
        10.0,
        0.0,
        106,
        "2026-05",
        10.0,
        monthly_series=(
            {"period": "2026-04", "value": 9.0},
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
            {"period": "2026-04", "market_size": 88_935.0},
            {"period": "2026-05", "market_size": 99_735.0},
        ),
        brands=brands,
        all_brands=brands + (focus,),
    )
    # When the cause payload is assembled.
    data = cause_payload.build_cause_data(
        definition=MarketDefinition(view="general", filter_echo={}, source="ubist", measure="sales"),
        metrics=metrics,
        focus=focus,
    )

    # Then the visible matrix remains top-100 scoped, while KPI uses the focus row.
    assert [row["brand_key"] for row in data["ei_ms_matrix"]["data"]] == ["b1", "b2", "b3", "b4", "b5"]
    assert [row["brand_key"] for row in data["growth_contribution_ms_matrix"]["data"]] == [
        "b1",
        "b2",
        "b3",
        "b4",
        "b5",
    ]
    assert data["ei_ms_matrix"]["data"][0]["value_recent"] == 999.0
    assert data["kpi"]["target_brand"] == "Focus"
    assert data["kpi"]["target_rank"] == 106
    assert data["kpi"]["brand_value_recent"] == 10.0


def test_build_cause_data_reports_missing_explicit_focus_without_market_leader_fallback() -> None:
    # Given a non-empty market where the explicitly requested focus is absent.
    brand = BrandMetric("leader", "Leader", "C10A1", 100.0, 100.0, 1, "2026-05", 100.0)
    metrics = AggregatedMetrics(
        source="ubist",
        measure="sales",
        unit_label="KRW",
        market_size=100.0,
        hhi=10_000.0,
        cagr=0.0,
        monthly_series=({"period": "2026-05", "market_size": 100.0},),
        brands=(brand,),
        all_brands=(brand,),
    )
    definition = MarketDefinition(
        view="general",
        filter_echo={},
        source="ubist",
        measure="sales",
        focus_brand_key="missing",
    )

    # When focus resolution and payload assembly run.
    focus = cause_payload._focus_brand(metrics.all_brands, definition.focus_brand_key)
    data = cause_payload.build_cause_data(definition=definition, metrics=metrics, focus=focus)

    # Then absence is explicit and the leader is never substituted.
    assert focus is None
    assert data["kpi"] == {}
    assert data["kpi_reason"] == "focus_not_found"


def test_build_cause_data_keeps_general_source_levels_with_focus_ml_market(monkeypatch) -> None:
    analysis_row = {
        "source": "ubist",
        "measure": "sales",
        "unit_label": "KRW",
        "by_dimension": json.dumps({"seller": "JW중외제약", "molecule_strength": "Sitagliptin 100mg"}, ensure_ascii=False),
        "dimension_data": json.dumps(
            {
                "seller": {"JW중외제약": {"2026-04": {"raw_value": 80.0}, "2026-05": {"raw_value": 100.0}}},
                "molecule_strength": {"Sitagliptin 100mg": {"2026-04": {"raw_value": 80.0}, "2026-05": {"raw_value": 100.0}}},
            },
            ensure_ascii=False,
        ),
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
            return [
                {
                    "brand_key": "focus",
                    "brand_name": "Focus",
                    "atc4_code": "A10N1",
                    "dimension_type": "molecule",
                    "dimension_value": "Sitagliptin / Metformin",
                    "raw_value_history": json.dumps(
                        {"2026-04": 80.0, "2026-05": 100.0},
                        ensure_ascii=False,
                    ),
                }
            ]
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
        period_range=PeriodRange("2026-05", "2026-05"),
    )

    assert any(call[1] == ("ml_999", "ubist", "sales") for call in calls)
    assert data["analysis_levels"]["levels"] == ["판매사", "성분", "성분용량", "제형", "투여경로", "급여구분"]
    assert "Class" not in data["analysis_levels"]["data"]
    seller_segments = data["analysis_levels"]["data"]["판매사"]["by_channel"]["전체"]
    ingredient_segments = data["analysis_levels"]["data"]["성분"]["by_channel"]["전체"]
    molecule_segments = data["analysis_levels"]["data"]["성분용량"]["by_channel"]["전체"]
    assert [item["name"] for item in seller_segments] == ["전체", "JW중외제약"]
    assert [item["name"] for item in ingredient_segments] == ["전체", "Sitagliptin / Metformin"]
    assert [item["name"] for item in molecule_segments] == ["전체", "Sitagliptin 100mg"]
    assert data["level_top5_trend"]["by_level"]["판매사"]["values"][1]["value"] == "JW중외제약"
    analysis_json = json.dumps(
        {
            "analysis_levels": data["analysis_levels"],
            "analysis_level_market_status": data["analysis_level_market_status"],
            "level_top5_trend": data["level_top5_trend"],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    assert "2026-04" not in analysis_json
    assert "2026-05" in analysis_json


@pytest.mark.parametrize(
    ("ml_id", "cd_market_id"),
    (("ml_006", None), (None, "cd_006")),
)
def test_strategic_runtime_trims_every_builder_row_before_ml_or_cd_assembly(
    monkeypatch,
    ml_id: str | None,
    cd_market_id: str | None,
) -> None:
    captured: dict[str, object] = {}
    sibling_rows = [
        {
            "brand_key": "리바로",
            "brand_name": "리바로",
            "metric_history": json.dumps(
                {"2025-01": {"raw_value": 10.0}, "2026-01": {"raw_value": 20.0}}
            ),
            "extended_metric_history": json.dumps(
                {"2025-01": {"raw_value": 10.0}, "2026-01": {"raw_value": 20.0}}
            ),
            "dimension_data": json.dumps(
                {"molecule": {"Pitavastatin": {"2025-01": 10.0, "2026-01": 20.0}}}
            ),
            "is_jw": 1,
        }
    ]
    market_row = {
        "market_size_series": json.dumps({"2025-01": 100.0, "2026-01": 200.0}),
        "hhi_series_5y": json.dumps(
            [{"year": 2025, "hhi": 100.0}, {"year": 2026, "hhi": 200.0}]
        ),
    }

    monkeypatch.setattr(strategic_runtime, "_fetch_sibling_rows", lambda **_kwargs: sibling_rows)
    monkeypatch.setattr(strategic_runtime, "_fetch_market_row", lambda **_kwargs: market_row)
    monkeypatch.setattr(strategic_runtime, "_catalog_row", lambda *_args: {})
    monkeypatch.setattr(strategic_runtime, "_strategic_brand_catalog", lambda: [])
    monkeypatch.setattr(strategic_runtime, "compose_cached_json", lambda payload, **_kwargs: payload)
    monkeypatch.setattr(strategic_runtime, "apply_cd_market_definition", lambda *_args: None)

    def fake_build_response(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"data": {"kpi": {"market_size_recent": 100.0}}}

    monkeypatch.setattr(strategic_runtime.cause_builder, "build_response", fake_build_response)

    strategic_runtime._build_strategic_payload(
        mart_db="mart",
        ml_id=ml_id,
        cd_market_id=cd_market_id,
        focus_brand_key="리바로",
        source="iqvia",
        measure="sales",
        analysis_level=DynamicMarketRequest().filters.analysis_level,
        period_range=PeriodRange("2025-01", "2025-12"),
    )

    captured_rows = captured["sibling_rows"]
    assert isinstance(captured_rows, list)
    assert json.loads(captured_rows[0]["metric_history"]) == {"2025-01": {"raw_value": 10.0}}
    assert json.loads(captured_rows[0]["dimension_data"])["molecule"]["Pitavastatin"] == {
        "2025-01": 10.0
    }
    captured_market = captured["market_row"]
    assert isinstance(captured_market, dict)
    assert json.loads(captured_market["market_size_series"]) == {"2025-01": 100.0}
    assert json.loads(captured_market["hhi_series_5y"]) == [{"hhi": 100.0, "year": 2025}]


def test_strategic_runtime_rejects_period_window_without_observed_points(monkeypatch) -> None:
    sibling_rows = [
        {
            "brand_key": "리바로",
            "brand_name": "리바로",
            "metric_history": json.dumps({"2026-01": {"raw_value": 20.0}}),
            "extended_metric_history": json.dumps({"2026-01": {"raw_value": 20.0}}),
            "is_jw": 1,
        }
    ]
    market_row = {"market_size_series": json.dumps({"2026-01": 200.0})}
    monkeypatch.setattr(strategic_runtime, "_fetch_sibling_rows", lambda **_kwargs: sibling_rows)
    monkeypatch.setattr(strategic_runtime, "_fetch_market_row", lambda **_kwargs: market_row)
    monkeypatch.setattr(strategic_runtime, "_catalog_row", lambda *_args: {})

    with pytest.raises(DynamicMarketPeriodNoDataError):
        strategic_runtime._build_strategic_payload(
            mart_db="mart",
            ml_id="ml_006",
            cd_market_id=None,
            focus_brand_key="리바로",
            source="iqvia",
            measure="sales",
            analysis_level=DynamicMarketRequest().filters.analysis_level,
            period_range=PeriodRange("2030-01", "2030-12"),
        )


def test_ubist_channel_summary_uses_superset_scope_with_pair_filter(monkeypatch, caplog) -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []

    def fake_fetch_all(sql: str, params: tuple[object, ...]) -> list[dict[str, object]]:
        calls.append((sql, params))
        return []

    def fake_iter_rows(sql: str, params: tuple[object, ...]):
        calls.append((sql, params))
        assert "(brand_key, atc4_code) IN" not in sql
        assert "brand_key IN" in sql
        assert "atc4_code IN" in sql
        yield {
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
            "audit_code_matrix": json.dumps({}),
        }
        yield {
            "brand_key": "a",
            "brand_name": "A-other-market",
            "atc4_code": "C10C0",
            "source": "ubist",
            "measure": "sales",
            "unit_label": "KRW",
            "raw_value_history": json.dumps({"2026-05": 999.0}),
            "channel_specialty_matrix": json.dumps(
                {"의원": {"가정의학과(FM)": {"2026-05": 999.0}}},
                ensure_ascii=False,
            ),
            "audit_code_matrix": json.dumps({}),
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

    assert len(calls) == 1
    metric_sql, metric_params = calls[0]
    assert "raw_value_history" in metric_sql
    assert metric_params == ("ubist", "sales", "a", "C10A1")
    assert metrics.ubist_specialty_channels == ("전체", "종합병원 순환기")
    assert "general_metric_rows_pair_filter filtered_rows=1" in caplog.text


def test_general_aggregate_slices_ubist_channel_axis_from_raw_matrix() -> None:
    request = DynamicMarketRequest.model_validate(
        {
            "source": "ubist",
            "measure": "sales",
            "filters": {
                "atc4": ["C10A1"],
                "analysis_level": {
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
        channel_axis=request.filters.analysis_level.to_channel_axis(source=request.source),
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


def test_unformatted_compose_has_identical_final_route_payload() -> None:
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
        market_size=100.12349,
        hhi=5000.56789,
        cagr=None,
        monthly_series=({"period": "2026-04", "market_size": 100.12349},),
        brands=(),
    )
    composer = ResponseComposer()

    formatted = composer.compose(definition=definition, metrics=metrics)
    unformatted = composer.compose_unformatted(definition=definition, metrics=metrics)

    assert compose_cached_json(
        {"status": "SUCCESS", "result": unformatted}, measure="sales"
    ) == compose_cached_json(
        {"status": "SUCCESS", "result": formatted}, measure="sales"
    )


def test_dynamic_cleaner_preserves_preformatted_matrix_derivations() -> None:
    raw = {
        "status": "SUCCESS",
        "result": {
            "growth_contribution_ms_matrix": [
                {"ms": 1.23459},
                {"ms": 2.34569},
            ],
            "market_size_series": {
                "2021-01": 100.12349,
                "2022-01": 110.98769,
            },
        },
    }
    expected = compose_cached_json(deep_format_numbers(raw), measure="sales")

    assert cache_to_response.compose_dynamic_json(raw, measure="sales") == expected


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
        def compose_unformatted(self, **_: object) -> dict:
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
        def compose_unformatted(self, **_: object) -> dict:
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
    assert data["ei_ms_matrix"] is data["growth_contribution_ms_matrix"]
    assert {"targets"}.issubset(data["target_customer_competition"])


def test_general_response_slimming_removes_only_approved_unused_fields() -> None:
    data = {
        "market_yoy_series": {"2026-01": 1.0},
        "market_yoy_recent_pct": 1.0,
        "ubist_specialty_channels": ["전체"],
        "ubist_specialty_target_channels": [],
        "target_customer_competition_by_channel": {"views": []},
        "hhi_recent": 1234.0,
        "level_top5_trend": {
            "by_level": {
                "판매사": {
                    "level_label": "판매사",
                    "level_value": "seller",
                    "total_market_value": 100.0,
                    "values": [
                        {
                            "value": "JW중외제약",
                            "value_recent_100m": 12.3,
                            "total_volume": 9.0,
                            "brands_in_value": [
                                {
                                    "brand": "리바로",
                                    "volume_recent": 3.0,
                                    "volume_series_10pt": [1.0, 2.0],
                                    "ms_recent_pct": 50.0,
                                }
                            ],
                        }
                    ],
                }
            }
        },
        "sources_data": {
            "market_yoy_series": {"2026-01": 1.0},
            "market_yoy_recent_pct": 1.0,
        },
    }

    slimmed = cause_payload.slim_general_response_data(data)

    for key in (
        "market_yoy_series",
        "market_yoy_recent_pct",
        "ubist_specialty_channels",
        "ubist_specialty_target_channels",
        "target_customer_competition_by_channel",
    ):
        assert key not in slimmed
    assert slimmed["hhi_recent"] == 1234.0
    assert slimmed["sources_data"] == data["sources_data"]
    level = slimmed["level_top5_trend"]["by_level"]["판매사"]
    assert "level_label" not in level
    assert "level_value" not in level
    assert "total_market_value" not in level
    value = level["values"][0]
    assert "total_volume" not in value
    assert value["value_recent_100m"] == 12.3
    brand = value["brands_in_value"][0]
    assert "volume_recent" not in brand
    assert "volume_series_10pt" not in brand
    assert brand["ms_recent_pct"] == 50.0


def test_general_dimension_aliases_defer_series_encoding_until_window_projection(monkeypatch) -> None:
    specs = general_analysis_levels.GENERAL_LEVEL_SPECS["ubist"]
    period_series = {
        "2025-05": {"raw_value": 10.0},
        "2025-06": {"raw_value": 20.0},
        "2026-05": {"raw_value": 30.0},
    }
    row = {
        "by_dimension": json.dumps({"seller": "JW중외제약"}, ensure_ascii=False),
        "dimension_data": json.dumps(
            {"seller": {"JW중외제약": period_series}},
            ensure_ascii=False,
        ),
        "dimension_channel_data": json.dumps(
            {"seller": {"JW중외제약": {"전체": period_series}}},
            ensure_ascii=False,
        ),
        "dimension_specialty_data": json.dumps(
            {"seller": {"JW중외제약": {"의원": {"내과": period_series}}}},
            ensure_ascii=False,
        ),
    }
    period_range = PeriodRange(start="2025-06", end="2026-05")
    expected = general_analysis_levels.trim_period_rows(
        [general_analysis_levels._with_canonical_dimension_aliases(row, specs)],
        period_range,
    )[0]
    dump_calls: list[dict[str, object]] = []
    json_dump = general_analysis_levels._json_dump

    def record_dump(payload: dict[str, object]) -> str:
        dump_calls.append(payload)
        return json_dump(payload)

    monkeypatch.setattr(general_analysis_levels, "_json_dump", record_dump)

    deferred = general_analysis_levels._with_canonical_dimension_aliases(
        row,
        specs,
        defer_period_series_encoding=True,
    )
    actual = general_analysis_levels.trim_period_rows([deferred], period_range)[0]

    assert len(dump_calls) == 1
    assert deferred["dimension_data"] == "{}"
    assert deferred["dimension_channel_data"] == "{}"
    assert deferred["dimension_specialty_data"] == "{}"
    assert actual == expected


def test_general_analysis_levels_leave_predecoded_dimension_json_unmaterialized(monkeypatch) -> None:
    class ProjectionObserved(Exception):
        pass

    monkeypatch.setattr(
        general_analysis_levels,
        "build_analysis_rows",
        lambda **_kwargs: [{"brand_key": "brand"}],
    )

    def observe_projection(
        _rows: object,
        _period_range: PeriodRange,
        *,
        materialize_predecoded_fields: bool = True,
    ) -> list[dict[str, object]]:
        assert materialize_predecoded_fields is False
        raise ProjectionObserved

    monkeypatch.setattr(general_analysis_levels, "trim_period_rows", observe_projection)

    with pytest.raises(ProjectionObserved):
        general_analysis_levels.build_general_analysis_level_sections(
            definition=SimpleNamespace(),
            metrics=SimpleNamespace(source="iqvia_nsa", measure="sales"),
            focus=None,
            mart_db="mart",
            period_range=PeriodRange("2025-01", "2025-12"),
        )


def test_general_channel_resolver_reuses_windowed_private_matrix(monkeypatch) -> None:
    private_matrix = {"의원": {"내과": {"2026-05": 30.0}}}
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        general_analysis_levels,
        "build_analysis_rows",
        lambda **_kwargs: [
            {
                "brand_key": "brand",
                "brand_name": "Brand",
                "channel_specialty_matrix": "{}",
                "__channel_specialty_matrix": private_matrix,
            }
        ],
    )

    def capture_resolver(**kwargs):
        captured.update(kwargs["rows"][0])
        raise RuntimeError("stop after resolver")

    monkeypatch.setattr(general_analysis_levels, "resolve_market_channels", capture_resolver)

    with pytest.raises(RuntimeError, match="stop after resolver"):
        general_analysis_levels.build_general_analysis_level_sections(
            definition=SimpleNamespace(market_catalog_row={}),
            metrics=SimpleNamespace(source="ubist", measure="sales"),
            focus=None,
            mart_db="mart",
            period_range=PeriodRange("2025-06", "2026-05"),
        )

    assert captured["channel_specialty_matrix"] == "{}"
    assert captured["__channel_specialty_matrix"] is private_matrix


def test_ubist_channel_resolver_prefers_private_matrix_over_empty_wire_field() -> None:
    private_matrix = {"의원": {"내과": {"2026-05": 30.0}}}
    rows = [
        {
            "brand_key": "brand",
            "brand_name": "Brand",
            "channel_specialty_matrix": "{}",
            "__channel_specialty_matrix": private_matrix,
        }
    ]

    assert ubist_channel_resolver._raw_matrices_available(rows)
    assert list(ubist_channel_resolver._iter_raw_matrix(rows[0])) == [
        ("의원", "내과", {"2026-05": 30.0})
    ]


def test_window_channel_specialty_matrix_drops_out_of_range_periods() -> None:
    matrix = {
        "의원": {
            "내과": {
                "2025-05": 10.0,
                "2025-06": 20.0,
                "2026-05": 30.0,
                "2026-06": 40.0,
            }
        }
    }

    result = aggregator_module._window_channel_specialty_matrix(
        matrix,
        PeriodRange("2025-06", "2026-05"),
    )

    assert result == {"의원": {"내과": {"2025-06": 20.0, "2026-05": 30.0}}}


def test_window_channel_specialty_matrix_reuses_unbounded_input() -> None:
    matrix = {"의원": {"내과": {"2026-05": 30.0}}}

    result = aggregator_module._window_channel_specialty_matrix(
        matrix,
        PeriodRange(None, None),
    )

    assert result is matrix


@pytest.mark.parametrize(
    "row",
    [
        {"by_dimension": "{}"},
        {
            "by_dimension": {},
            "dimension_data": {
                "seller": {"JW중외제약": {"2026-05": {"raw_value": 30.0}}}
            },
            "dimension_channel_data": {},
            "dimension_specialty_data": {},
        },
    ],
)
def test_general_deferred_dimension_encoding_preserves_sparse_and_decoded_shapes(
    row: dict[str, object],
) -> None:
    specs = general_analysis_levels.GENERAL_LEVEL_SPECS["ubist"]
    period_range = PeriodRange(start="2025-06", end="2026-05")

    expected = general_analysis_levels.trim_period_rows(
        [general_analysis_levels._with_canonical_dimension_aliases(row, specs)],
        period_range,
    )[0]
    actual = general_analysis_levels.trim_period_rows(
        [
            general_analysis_levels._with_canonical_dimension_aliases(
                row,
                specs,
                defer_period_series_encoding=True,
            )
        ],
        period_range,
    )[0]

    assert actual == expected


def test_cause_payload_uses_source_specific_levels_for_general_ubist(monkeypatch) -> None:
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
    class_channel_series = {
        "상급종합병원": {"2026-01": {"raw_value": 30.0}, "2026-02": {"raw_value": 40.0}},
        "종합병원": class_series,
        "병원": {"2026-01": {"raw_value": 500.0}, "2026-02": {"raw_value": 600.0}},
        "의원": {"2026-01": {"raw_value": 10.0}, "2026-02": {"raw_value": 20.0}},
    }
    specialty_matrix = {
        "종합병원": {"순환기(Cardiology IM)": {"2026-01": 50.0, "2026-02": 70.0}},
        "의원": {"가정의학과(FM)": {"2026-01": 10.0, "2026-02": 20.0}},
    }
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
            "by_dimension": json.dumps(
                {
                    "seller": "JW중외제약",
                    "molecule_strength": "Anagliptin 100mg",
                    "form": "정제",
                    "route": "경구",
                    "reimbursement": "급여",
                },
                ensure_ascii=False,
            ),
            "dimension_data": json.dumps(
                {
                    "seller": {"JW중외제약": class_series},
                    "molecule_strength": {"Anagliptin 100mg": class_series},
                    "form": {"정제": class_series},
                    "route": {"경구": class_series},
                    "reimbursement": {"급여": class_series},
                },
                ensure_ascii=False,
            ),
            "dimension_channel_data": json.dumps({"seller": {"JW중외제약": class_channel_series}}, ensure_ascii=False),
            "channel_data": json.dumps(class_channel_series, ensure_ascii=False),
            "channel_specialty_matrix": json.dumps(specialty_matrix, ensure_ascii=False),
        },
        channel_specialty_matrix=specialty_matrix,
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
            "by_dimension": json.dumps(
                {
                    "seller": "Competitor Co",
                    "molecule_strength": "Other 100mg",
                    "form": "정제",
                    "route": "경구",
                    "reimbursement": "급여",
                },
                ensure_ascii=False,
            ),
            "dimension_data": json.dumps(
                {
                    "seller": {"Competitor Co": {"2026-01": {"raw_value": 100.0}, "2026-02": {"raw_value": 80.0}}},
                    "molecule_strength": {"Other 100mg": {"2026-01": {"raw_value": 100.0}, "2026-02": {"raw_value": 80.0}}},
                    "form": {"정제": {"2026-01": {"raw_value": 100.0}, "2026-02": {"raw_value": 80.0}}},
                    "route": {"경구": {"2026-01": {"raw_value": 100.0}, "2026-02": {"raw_value": 80.0}}},
                    "reimbursement": {"급여": {"2026-01": {"raw_value": 100.0}, "2026-02": {"raw_value": 80.0}}},
                },
                ensure_ascii=False,
            ),
            "dimension_channel_data": json.dumps({}, ensure_ascii=False),
            "channel_data": json.dumps({"종합병원": {"2026-01": {"raw_value": 50.0}, "2026-02": {"raw_value": 40.0}}}, ensure_ascii=False),
            "channel_specialty_matrix": json.dumps(
                {"종합병원": {"순환기(Cardiology IM)": {"2026-01": 60.0, "2026-02": 40.0}}},
                ensure_ascii=False,
            ),
        },
        channel_specialty_matrix={"종합병원": {"순환기(Cardiology IM)": {"2026-01": 60.0, "2026-02": 40.0}}},
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
    assert analysis_levels["levels"] == ["판매사", "성분", "성분용량", "제형", "투여경로", "급여구분"]
    assert analysis_levels["channels"] == ["전체", "상급종병", "종병", "(상급종병 + 종병)", "병원", "의원", "보건소", "기타"]
    assert any(
        segment["name"] == "JW중외제약"
        for segment in analysis_levels["data"]["판매사"]["by_channel"]["전체"]
    )
    seller_by_channel = analysis_levels["data"]["판매사"]["by_channel"]
    assert seller_by_channel["종병"]
    tgh_segment = next(segment for segment in seller_by_channel["(상급종병 + 종병)"] if segment["name"] == "JW중외제약")
    th_segment = next(segment for segment in seller_by_channel["상급종병"] if segment["name"] == "JW중외제약")
    gh_segment = next(segment for segment in seller_by_channel["종병"] if segment["name"] == "JW중외제약")
    hospital_segment = next(segment for segment in seller_by_channel["병원"] if segment["name"] == "JW중외제약")
    assert tgh_segment["value_series"] == [
        th_segment["value_series"][0] + gh_segment["value_series"][0],
        th_segment["value_series"][1] + gh_segment["value_series"][1],
    ]
    assert hospital_segment["value_series"] == [500.0, 600.0]
    assert tgh_segment["value_series"] == [130.0, 160.0]
    assert payload["data"]["analysis_level_market_status"]["channels"] == ["전체", "주요고객 종합병원 순환기", "의원 IGF"]
    assert "상급종병" not in payload["data"]["analysis_level_market_status"]["data"]["판매사"]["by_channel"]
    assert payload["data"]["analysis_level_market_status"]["data"]["판매사"]["by_channel"]["주요고객 종합병원 순환기"]
    target_competition = payload["data"]["target_customer_competition"]
    assert "주요고객 종합병원 순환기" in target_competition["targets"]
    assert any(view["target_name"] == "주요고객 종합병원 순환기" for view in target_competition["views"])
    assert "target_customer_competition_by_channel" not in payload["data"]
    assert "ubist_specialty_channels" not in payload["data"]
    assert payload["data"]["level_top5_trend"]["default_level"] == "판매사"
    assert payload["data"]["level_top5_trend"]["by_level"]["판매사"]["values"]


def test_cause_payload_uses_iqvia_source_levels_without_pack_desc(monkeypatch) -> None:
    monkeypatch.setattr("pipeline.scripts.api.dynamic_market.analysis_level_dimensions.db.fetch_all", lambda *_args: [])

    definition = MarketDefinition(
        view="general",
        filter_echo={"view": "general", "atc4": ["A10B1"], "source": "iqvia_nsa", "measure": "sales"},
        source="iqvia_nsa",
        measure="sales",
    )
    class_series = {"2026-Q1": {"raw_value": 100.0}, "2026-Q2": {"raw_value": 120.0}}
    focus = BrandMetric(
        "focus",
        "Focus Brand",
        "A10B1",
        120.0,
        60.0,
        1,
        "2026-Q2",
        120.0,
        ({"period": "2026-Q1", "value": 100.0}, {"period": "2026-Q2", "value": 120.0}),
        history_by_period={"2026-Q1": 100.0, "2026-Q2": 120.0},
        analysis_row={
            "by_dimension": json.dumps(
                {
                    "mfr": "JW중외제약",
                    "molecule_type": "SMALL MOLECULE",
                    "molecule_desc": "ANAGLIPTIN",
                    "strength": "100MG",
                    "nhi": "급여",
                },
                ensure_ascii=False,
            ),
            "dimension_data": json.dumps(
                {
                    "mfr": {"JW중외제약": class_series},
                    "molecule_type": {"SMALL MOLECULE": class_series},
                    "molecule_desc": {"ANAGLIPTIN": class_series},
                    "strength": {"100MG": class_series},
                    "nhi": {"급여": class_series},
                },
                ensure_ascii=False,
            ),
            "dimension_channel_data": json.dumps({}, ensure_ascii=False),
            "channel_data": json.dumps({}, ensure_ascii=False),
        },
    )
    metrics = AggregatedMetrics(
        source="iqvia_nsa",
        measure="sales",
        unit_label="KRW",
        market_size=200.0,
        hhi=None,
        cagr=None,
        monthly_series=({"period": "2026-Q1", "market_size": 100.0}, {"period": "2026-Q2", "market_size": 200.0}),
        brands=(focus,),
        all_brands=(focus,),
    )

    payload = build_cause_payload(definition=definition, metrics=metrics)

    expected_levels = ["MFR NAME KOR", "MOLECULE TYPE", "MOLECULE DESC", "STRENGTH", "NHI TYPE"]
    assert payload["data"]["analysis_levels"]["levels"] == expected_levels
    assert payload["data"]["level_top5_trend"]["default_level"] == "MFR NAME KOR"
    assert list(payload["data"]["level_top5_trend"]["by_level"]) == expected_levels
    assert "PACK DESC" not in payload["data"]["analysis_levels"]["data"]


def test_cause_payload_uses_iqvia_sidecar_values_over_canonical_dimensions(monkeypatch) -> None:
    def fake_fetch_all(sql: str, params: object = ()) -> list[dict[str, object]]:
        if "mart_general_filter_dimension_metric" in sql:
            return [
                {
                    "brand_key": "focus",
                    "brand_name": "Focus Brand",
                    "atc4_code": "A10B1",
                    "dimension_type": "mfr",
                    "dimension_value": "JW중외제약",
                    "raw_value_history": json.dumps({"2026-Q1": 100.0, "2026-Q2": 120.0}),
                },
                {
                    "brand_key": "focus",
                    "brand_name": "Focus Brand",
                    "atc4_code": "A10B1",
                    "dimension_type": "molecule_type",
                    "dimension_value": "SINGLE",
                    "raw_value_history": json.dumps({"2026-Q1": 100.0, "2026-Q2": 120.0}),
                },
                {
                    "brand_key": "focus",
                    "brand_name": "Focus Brand",
                    "atc4_code": "A10B1",
                    "dimension_type": "molecule_desc",
                    "dimension_value": "ANAGLIPTIN",
                    "raw_value_history": json.dumps({"2026-Q1": 100.0, "2026-Q2": 120.0}),
                },
                {
                    "brand_key": "focus",
                    "brand_name": "Focus Brand",
                    "atc4_code": "A10B1",
                    "dimension_type": "strength",
                    "dimension_value": "100MG",
                    "raw_value_history": json.dumps({"2026-Q1": 100.0, "2026-Q2": 120.0}),
                },
                {
                    "brand_key": "focus",
                    "brand_name": "Focus Brand",
                    "atc4_code": "A10B1",
                    "dimension_type": "nhi",
                    "dimension_value": "NHI",
                    "raw_value_history": json.dumps({"2026-Q1": 100.0, "2026-Q2": 120.0}),
                },
            ]
        if "mart_general_brand_metric" in sql:
            return [
                {
                    "brand_key": "focus",
                    "brand_name": "Focus Brand",
                    "atc4_code": "A10B1",
                    "source": "iqvia_nsa",
                    "measure": "sales",
                    "unit_label": "KRW",
                    "by_dimension": json.dumps(
                        {
                            "company": "JW중외제약",
                            "molecule": "TENELIGLIPTIN",
                            "dosage_form": "Oral Solid Ordinary Tablets",
                            "nhi_type": "NON-NHI",
                        },
                        ensure_ascii=False,
                    ),
                    "dimension_data": json.dumps(
                        {
                            "molecule": {"TENELIGLIPTIN": {"2026-Q1": {"raw_value": 100.0}, "2026-Q2": {"raw_value": 120.0}}},
                            "dosage_form": {
                                "Oral Solid Ordinary Tablets": {"2026-Q1": {"raw_value": 100.0}, "2026-Q2": {"raw_value": 120.0}}
                            },
                            "nhi_type": {"NON-NHI": {"2026-Q1": {"raw_value": 100.0}, "2026-Q2": {"raw_value": 120.0}}},
                        },
                        ensure_ascii=False,
                    ),
                    "dimension_channel_data": json.dumps({}, ensure_ascii=False),
                    "channel_data": json.dumps({}, ensure_ascii=False),
                    "channel_specialty_matrix": json.dumps({}, ensure_ascii=False),
                }
            ]
        return []

    monkeypatch.setattr("pipeline.scripts.api.dynamic_market.analysis_level_dimensions.db.fetch_all", fake_fetch_all)

    definition = MarketDefinition(
        view="general",
        filter_echo={"view": "general", "atc4": ["A10B1"], "source": "iqvia_nsa", "measure": "sales"},
        source="iqvia_nsa",
        measure="sales",
    )
    focus = BrandMetric(
        "focus",
        "Focus Brand",
        "A10B1",
        120.0,
        60.0,
        1,
        "2026-Q2",
        120.0,
        ({"period": "2026-Q1", "value": 100.0}, {"period": "2026-Q2", "value": 120.0}),
        history_by_period={"2026-Q1": 100.0, "2026-Q2": 120.0},
        analysis_row={"by_dimension": "{}", "dimension_data": "{}", "dimension_channel_data": "{}", "channel_data": "{}"},
    )
    metrics = AggregatedMetrics(
        source="iqvia_nsa",
        measure="sales",
        unit_label="KRW",
        market_size=200.0,
        hhi=None,
        cagr=None,
        monthly_series=({"period": "2026-Q1", "market_size": 100.0}, {"period": "2026-Q2", "market_size": 200.0}),
        brands=(focus,),
        all_brands=(focus,),
    )

    payload = build_cause_payload(definition=definition, metrics=metrics)

    by_level = payload["data"]["level_top5_trend"]["by_level"]
    assert by_level["MFR NAME KOR"]["all_options"] == ["전체", "JW중외제약"]
    assert by_level["MOLECULE TYPE"]["all_options"] == ["전체", "SINGLE"]
    assert by_level["MOLECULE DESC"]["all_options"] == ["전체", "ANAGLIPTIN"]
    assert by_level["STRENGTH"]["all_options"] == ["전체", "100MG"]
    assert by_level["NHI TYPE"]["all_options"] == ["전체", "NHI"]
    assert "TENELIGLIPTIN" not in by_level["MOLECULE TYPE"]["all_options"]
    assert "Oral Solid Ordinary Tablets" not in by_level["MOLECULE DESC"]["all_options"]
    assert "NON-NHI" not in by_level["STRENGTH"]["all_options"]


def test_cause_payload_uses_ubist_sidecar_values_for_source_levels(monkeypatch) -> None:
    queries: list[tuple[str, object]] = []

    def fake_fetch_all(sql: str, params: object = ()) -> list[dict[str, object]]:
        queries.append((sql, params))
        if "mart_general_filter_dimension_metric" in sql:
            return [
                {
                    "brand_key": "focus",
                    "brand_name": "Focus Brand",
                    "atc4_code": "A10N1",
                    "dimension_type": "seller",
                    "dimension_value": "JW중외제약",
                    "raw_value_history": json.dumps({"2026-01": 100.0, "2026-02": 120.0}),
                },
                {
                    "brand_key": "focus",
                    "brand_name": "Focus Brand",
                    "atc4_code": "A10N1",
                    "dimension_type": "molecule_strength",
                    "dimension_value": "PITAVASTATIN 2MG",
                    "raw_value_history": json.dumps({"2026-01": 100.0, "2026-02": 120.0}),
                },
                {
                    "brand_key": "focus",
                    "brand_name": "Focus Brand",
                    "atc4_code": "A10N1",
                    "dimension_type": "form",
                    "dimension_value": "정제",
                    "raw_value_history": json.dumps({"2026-01": 100.0, "2026-02": 120.0}),
                },
                {
                    "brand_key": "focus",
                    "brand_name": "Focus Brand",
                    "atc4_code": "A10N1",
                    "dimension_type": "route",
                    "dimension_value": "경구",
                    "raw_value_history": json.dumps({"2026-01": 100.0, "2026-02": 120.0}),
                },
                {
                    "brand_key": "focus",
                    "brand_name": "Focus Brand",
                    "atc4_code": "A10N1",
                    "dimension_type": "reimbursement",
                    "dimension_value": "급여",
                    "raw_value_history": json.dumps({"2026-01": 100.0, "2026-02": 120.0}),
                },
            ]
        if "mart_general_brand_metric" in sql:
            return [
                {
                    "brand_key": "focus",
                    "brand_name": "Focus Brand",
                    "atc4_code": "A10N1",
                    "source": "ubist",
                    "measure": "sales",
                    "unit_label": "KRW",
                    "by_dimension": "{}",
                    "dimension_data": "{}",
                    "dimension_channel_data": "{}",
                    "channel_data": "{}",
                    "channel_specialty_matrix": "{}",
                }
            ]
        return []

    monkeypatch.setattr("pipeline.scripts.api.dynamic_market.analysis_level_dimensions.db.fetch_all", fake_fetch_all)

    definition = MarketDefinition(
        view="general",
        filter_echo={"view": "general", "atc4": ["A10N1"], "source": "ubist", "measure": "sales"},
        source="ubist",
        measure="sales",
    )
    focus = BrandMetric(
        "focus",
        "Focus Brand",
        "A10N1",
        120.0,
        60.0,
        1,
        "2026-02",
        120.0,
        ({"period": "2026-01", "value": 100.0}, {"period": "2026-02", "value": 120.0}),
        history_by_period={"2026-01": 100.0, "2026-02": 120.0},
        analysis_row={"by_dimension": "{}", "dimension_data": "{}", "dimension_channel_data": "{}", "channel_data": "{}"},
    )
    metrics = AggregatedMetrics(
        source="ubist",
        measure="sales",
        unit_label="KRW",
        market_size=200.0,
        hhi=None,
        cagr=None,
        monthly_series=({"period": "2026-01", "market_size": 100.0}, {"period": "2026-02", "market_size": 200.0}),
        brands=(focus,),
        all_brands=(focus,),
    )

    payload = build_cause_payload(definition=definition, metrics=metrics)

    by_level = payload["data"]["level_top5_trend"]["by_level"]
    assert by_level["판매사"]["all_options"] == ["전체", "JW중외제약"]
    assert by_level["성분용량"]["all_options"] == ["전체", "PITAVASTATIN 2MG"]
    assert by_level["제형"]["all_options"] == ["전체", "정제"]
    assert by_level["투여경로"]["all_options"] == ["전체", "경구"]
    assert by_level["급여구분"]["all_options"] == ["전체", "급여"]
    assert all(by_level[level]["values"] for level in ["판매사", "성분용량", "제형", "투여경로", "급여구분"])
    analysis_levels = payload["data"]["analysis_levels"]
    assert analysis_levels["data"]["판매사"]["segments"]
    assert analysis_levels["data"]["성분용량"]["segments"]
    assert any("mart_general_filter_dimension_metric" in sql for sql, _params in queries)



@pytest.mark.parametrize(
    ("source", "atc4", "periods", "level", "fields"),
    [
        (
            "ubist",
            "A10N1",
            ("2026-01", "2026-02", "2026-03"),
            "판매사",
            ("seller", "JW중외제약", "경쟁사"),
        ),
        (
            "iqvia_nsa",
            "C10A1",
            ("2026-Q1", "2026-Q2", "2026-Q3"),
            "MFR NAME KOR",
            ("mfr", "JW중외제약", "경쟁사"),
        ),
    ],
)
def test_general_source_level_current_share_uses_latest_valid_sidecar_period(
    monkeypatch,
    source: str,
    atc4: str,
    periods: tuple[str, str, str],
    level: str,
    fields: tuple[str, str, str],
) -> None:
    monkeypatch.setattr("pipeline.scripts.api.dynamic_market.analysis_level_dimensions.db.fetch_all", lambda *_args: [])
    field, focus_value, competitor_value = fields

    def make_brand(key: str, name: str, values: tuple[float, float, float], dimension_value: str) -> BrandMetric:
        return BrandMetric(
            key,
            name,
            atc4,
            0.0,
            0.0,
            1 if key == "focus" else 2,
            periods[-1],
            0.0,
            tuple({"period": period, "value": value} for period, value in zip(periods, values)),
            history_by_period=dict(zip(periods, values)),
            analysis_row={
                "by_dimension": json.dumps({field: dimension_value}, ensure_ascii=False),
                "dimension_data": json.dumps(
                    {field: {dimension_value: {period: {"raw_value": value} for period, value in zip(periods, values)}}},
                    ensure_ascii=False,
                ),
                "dimension_channel_data": "{}",
                "channel_data": "{}",
            },
        )

    definition = MarketDefinition(
        view="general",
        filter_echo={"view": "general", "atc4": [atc4], "source": source, "measure": "sales"},
        source=source,
        measure="sales",
    )
    metrics = AggregatedMetrics(
        source=source,
        measure="sales",
        unit_label="KRW",
        market_size=0.0,
        hhi=None,
        cagr=None,
        monthly_series=tuple(
            {"period": period, "market_size": value} for period, value in zip(periods, (200.0, 200.0, 0.0))
        ),
        brands=(
            make_brand("focus", "Focus Brand", (100.0, 120.0, 0.0), focus_value),
            make_brand("competitor", "Competitor Brand", (100.0, 80.0, 0.0), competitor_value),
        ),
        all_brands=(
            make_brand("focus", "Focus Brand", (100.0, 120.0, 0.0), focus_value),
            make_brand("competitor", "Competitor Brand", (100.0, 80.0, 0.0), competitor_value),
        ),
    )

    payload = build_cause_payload(definition=definition, metrics=metrics)

    segments = payload["data"]["analysis_levels"]["data"][level]["by_channel"]["전체"]
    focus_segment = next(segment for segment in segments if segment["name"] == focus_value)
    competitor_segment = next(segment for segment in segments if segment["name"] == competitor_value)
    assert focus_segment["value_series"] == [100.0, 120.0, 0.0]
    assert competitor_segment["value_series"] == [100.0, 80.0, 0.0]
    assert focus_segment["series_pct"] == [50.0, 60.0, 0.0]
    assert focus_segment["recent_share_pct"] == 60.0
    assert competitor_segment["recent_share_pct"] == 40.0

    status_segment = next(
        segment
        for segment in payload["data"]["analysis_level_market_status"]["data"][level]["by_channel"]["전체"]
        if segment["name"] == focus_value
    )
    assert status_segment["recent_share_pct"] == 60.0

    trend_values = payload["data"]["level_top5_trend"]["by_level"][level]["values"]
    trend_focus = next(value for value in trend_values if value["value"] == focus_value)
    trend_competitor = next(value for value in trend_values if value["value"] == competitor_value)
    assert trend_focus["ms_pct"] == 60.0
    assert trend_competitor["ms_pct"] == 40.0
    assert trend_focus["brands_in_value"][0]["ms_recent_pct"] == 100.0

def test_general_ubist_specialty_uses_market_catalog_for_channel_resolution(monkeypatch) -> None:
    captured_markets: list[dict[str, object]] = []
    build_calls = 0
    monkeypatch.setattr("pipeline.scripts.api.dynamic_market.analysis_level_dimensions.db.fetch_all", lambda *_args: [])

    original_build = general_analysis_levels.cause_builder._build_analysis_levels_from_mart

    def count_builds(*args: object, **kwargs: object) -> dict[str, object]:
        nonlocal build_calls
        build_calls += 1
        return original_build(*args, **kwargs)

    monkeypatch.setattr(general_analysis_levels.cause_builder, "_build_analysis_levels_from_mart", count_builds)

    def fake_resolve_market_channels(*, rows: object, market: dict[str, object], measure: str) -> dict[str, object]:
        captured_markets.append(dict(market))
        return {
            "specialty_channels": ["전체", "주요고객 종합병원 내분비", "의원 IGF"],
            "specialty_target_channels": [
                {"code": "TGH_ENDO", "display_name": "주요고객 종합병원 내분비"},
                {"code": "CLINIC_IGF", "display_name": "의원 IGF"},
            ],
        }

    monkeypatch.setattr(
        "pipeline.scripts.api.dynamic_market.general_analysis_levels.resolve_market_channels",
        fake_resolve_market_channels,
    )

    market_catalog_row = {
        "ml_id": "ml_006",
        "target_customer": "주요고객 종합병원 내분비",
        "target_customers": [{"display_name": "주요고객 종합병원 내분비"}],
    }
    definition = MarketDefinition(
        view="general",
        filter_echo={"view": "general", "atc4": ["A10N1"], "source": "ubist", "measure": "sales"},
        source="ubist",
        measure="sales",
        market_catalog_row=market_catalog_row,
    )
    focus = BrandMetric(
        "focus",
        "Focus Brand",
        "A10N1",
        120.0,
        60.0,
        1,
        "2026-02",
        120.0,
        ({"period": "2026-01", "value": 100.0}, {"period": "2026-02", "value": 120.0}),
        history_by_period={"2026-01": 100.0, "2026-02": 120.0},
        analysis_row={
            "by_dimension": json.dumps({"seller": "JW중외제약"}, ensure_ascii=False),
            "dimension_data": json.dumps(
                {"seller": {"JW중외제약": {"2026-01": {"raw_value": 100.0}, "2026-02": {"raw_value": 120.0}}}},
                ensure_ascii=False,
            ),
            "dimension_channel_data": "{}",
            "channel_data": "{}",
        },
    )
    metrics = AggregatedMetrics(
        source="ubist",
        measure="sales",
        unit_label="KRW",
        market_size=200.0,
        hhi=None,
        cagr=None,
        monthly_series=({"period": "2026-01", "market_size": 100.0}, {"period": "2026-02", "market_size": 200.0}),
        brands=(focus,),
        all_brands=(focus,),
        ubist_specialty_channels=("전체", "종합병원 내분비"),
        ubist_specialty_target_channels=(
            {"code": "TGH_ENDO", "display_name": "종합병원 내분비"},
        ),
    )

    payload = build_cause_payload(definition=definition, metrics=metrics)

    assert captured_markets == [market_catalog_row]
    assert build_calls == 1
    assert payload["data"]["analysis_level_market_status"]["channels"] == ["전체", "주요고객 종합병원 내분비", "의원 IGF"]


def test_cause_payload_builds_iqvia_target_customer_from_audit_channels(monkeypatch) -> None:
    monkeypatch.setattr("pipeline.scripts.api.dynamic_market.analysis_level_dimensions.db.fetch_all", lambda *_args: [])

    definition = MarketDefinition(
        view="general",
        filter_echo={"view": "general", "atc4": ["A10B1"], "source": "iqvia_nsa", "measure": "sales"},
        source="iqvia_nsa",
        measure="sales",
    )
    focus = BrandMetric(
        "focus",
        "Focus Brand",
        "A10B1",
        120.0,
        60.0,
        1,
        "2026-Q2",
        120.0,
        ({"period": "2026-Q1", "value": 100.0}, {"period": "2026-Q2", "value": 120.0}),
        history_by_period={"2026-Q1": 100.0, "2026-Q2": 120.0},
        analysis_row={
            "by_dimension": json.dumps({"mfr": "JW중외제약"}, ensure_ascii=False),
            "dimension_data": json.dumps({"mfr": {"JW중외제약": {"2026-Q1": {"raw_value": 100.0}, "2026-Q2": {"raw_value": 120.0}}}}, ensure_ascii=False),
            "dimension_channel_data": json.dumps({}, ensure_ascii=False),
            "channel_data": json.dumps(
                {
                    "KHPA": {"2026-Q1": {"raw_value": 40.0}, "2026-Q2": {"raw_value": 50.0}},
                    "KCPA": {"2026-Q1": {"raw_value": 20.0}, "2026-Q2": {"raw_value": 30.0}},
                    "KPA": {"2026-Q1": {"raw_value": 40.0}, "2026-Q2": {"raw_value": 40.0}},
                },
                ensure_ascii=False,
            ),
        },
        audit_code_matrix={
            "KHPA": {"2026-Q1": 40.0, "2026-Q2": 50.0},
            "KCPA": {"2026-Q1": 20.0, "2026-Q2": 30.0},
            "KPA": {"2026-Q1": 40.0, "2026-Q2": 40.0},
        },
    )
    metrics = AggregatedMetrics(
        source="iqvia_nsa",
        measure="sales",
        unit_label="KRW",
        market_size=200.0,
        hhi=None,
        cagr=None,
        monthly_series=({"period": "2026-Q1", "market_size": 100.0}, {"period": "2026-Q2", "market_size": 200.0}),
        brands=(focus,),
        all_brands=(focus,),
    )

    payload = build_cause_payload(definition=definition, metrics=metrics)

    target_competition = payload["data"]["target_customer_competition"]
    assert target_competition["targets"] == ["전체", "KHPA", "KCPA", "KPA"]
    assert [view["target_name"] for view in target_competition["views"]] == ["전체", "KHPA", "KCPA", "KPA"]
    assert "target_customer_competition_by_channel" not in payload["data"]


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


def test_build_dimension_filters_accepts_raw_ubist_molecule() -> None:
    filters = resolvers.build_dimension_filters(
        analysis_level={"ubist": {"molecule": ["PITAVASTATIN / EZETIMIBE"]}},
        source="ubist",
    )

    assert filters == (DimensionFilter("molecule", ("PITAVASTATIN / EZETIMIBE",)),)


def test_build_dimension_filters_accepts_ubist_atc_narrowing_dimensions() -> None:
    filters = resolvers.build_dimension_filters(
        analysis_level={"ubist": {"atc3": ["A10N"], "atc4": ["A10N1", "A10N3"]}},
        source="ubist",
    )

    assert filters == (
        DimensionFilter("atc3", ("A10N",)),
        DimensionFilter("atc4", ("A10N1", "A10N3")),
    )


def test_build_dimension_filters_accepts_iqvia_pack_desc_dimension() -> None:
    filters = resolvers.build_dimension_filters(
        analysis_level={"iqvia": {"pack_desc": ["PFS 162MG/0.9ML"]}},
        source="iqvia_nsa",
    )

    assert filters == (DimensionFilter("pack", ("PFS 162MG/0.9ML",)),)


def test_general_resolver_keeps_pack_desc_filter_exact_for_focus_brand(monkeypatch) -> None:
    def fake_fetch_all(sql: str, params: tuple[str, ...]) -> list[dict]:
        assert "mart_general_filter_dimension_metric" not in sql
        assert params == ["iqvia_nsa", "sales", "M01C0"]
        return [{"brand_key": "악템라", "brand_name": "악템라", "atc4_code": "M01C0"}]

    monkeypatch.setattr("pipeline.scripts.api.dynamic_market.resolvers.db.fetch_all", fake_fetch_all)
    monkeypatch.setattr("pipeline.scripts.api.dynamic_market.resolvers.db.fetch_one", lambda *_args, **_kwargs: None)

    definition = GeneralViewResolver(mart_db="jw_mart", bridge_db="jw_mart").resolve(
        atc4=["M01C0"],
        molecule=[],
        analysis_level={"iqvia": {"pack_desc": ["INFU.VIAL 200MG 10ML"]}},
        focus_brand_key="악템라",
        source="iqvia",
        measure="sales",
    )

    assert definition.dimension_filters == (DimensionFilter("pack", ("INFU.VIAL 200MG 10ML",)),)


def test_general_resolver_rejects_unknown_atc4_instead_of_returning_empty_market(monkeypatch) -> None:
    monkeypatch.setattr("pipeline.scripts.api.dynamic_market.resolvers.db.fetch_all", lambda *_args, **_kwargs: [])

    try:
        GeneralViewResolver(mart_db="jw_mart", bridge_db="jw_mart").resolve(
            atc4=["ZZZ999"],
            molecule=[],
            analysis_level=None,
            focus_brand_key=None,
            source="ubist",
            measure="sales",
        )
    except DynamicMarketInputError as exc:
        assert "general market rows were not found" in str(exc)
    else:
        raise AssertionError("unknown ATC4 should not produce a silent empty market")


def test_strategic_resolver_accepts_ubist_atc4_narrowing_dimension(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_fetch_all(sql: str, params: tuple[str, ...]) -> list[dict]:
        assert params[:3] == ("ml_003", "ubist", "sales")
        return [{"brand_key": "guardlet", "brand_name": "가드렛", "atc4_code": ""}]

    monkeypatch.setattr("pipeline.scripts.api.dynamic_market.resolvers.db.fetch_all", fake_fetch_all)
    monkeypatch.setattr("pipeline.scripts.api.dynamic_market.resolvers.db.fetch_one", lambda *_args, **_kwargs: {"ml_id": "ml_003"})

    definition = StrategicViewResolver(mart_db="jw_mart").resolve(
        view_kind="market_landscape",
        ml_id="ml_003",
        cd_market_id=None,
        atc4=[],
        molecule=[],
        analysis_level={"ubist": {"atc4": ["A10N1", "A10N3", "A10N9"]}},
        focus_brand_key="가드렛",
        source="ubist",
        measure="sales",
    )

    assert definition.dimension_filters == (DimensionFilter("atc4", ("A10N1", "A10N3", "A10N9")),)
    assert definition.filter_echo["analysis_level"] == {"atc4": ["A10N1", "A10N3", "A10N9"]}


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


def test_default_focus_brand_uses_all_atc4_when_brand_spans_multiple_buckets(monkeypatch) -> None:
    resolver = GeneralViewResolver(mart_db="jw_mart", bridge_db="jw_mart")
    brand_query_params: list[str] = []

    def fake_fetch_all(sql: str, params: tuple[str, ...] | list[str]) -> list[dict]:
        if "SELECT DISTINCT atc4_code" in sql:
            return [{"atc4_code": "A10A1"}, {"atc4_code": "A10A3"}]
        brand_query_params.extend(params)
        return [
            {"brand_key": "brand-a", "brand_name": "Brand A", "atc4_code": "A10A1"},
            {"brand_key": "brand-a", "brand_name": "Brand A", "atc4_code": "A10A3"},
        ]

    monkeypatch.setattr("pipeline.scripts.api.dynamic_market.resolvers.db.fetch_all", fake_fetch_all)
    definition = resolver.resolve(
        atc4=[],
        molecule=[],
        analysis_level=None,
        focus_brand_key="brand-a",
        source="ubist",
        measure="sales",
    )

    assert definition.filter_echo["atc4"] == ["A10A1", "A10A3"]
    assert brand_query_params == ["ubist", "sales", "A10A1", "A10A3"]
    assert [(brand.brand_key, brand.atc4_code) for brand in definition.brands] == [
        ("brand-a", "A10A1"),
        ("brand-a", "A10A3"),
    ]


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


def test_empty_analysis_level_channel_payloads_normalize_like_missing_filter() -> None:
    payloads = [
        {"filters": {"atc4": ["C10A1"]}, "source": "ubist", "measure": "sales"},
        {"filters": {"atc4": ["C10A1"], "analysis_level": {"ubist": {}}}, "source": "ubist", "measure": "sales"},
        {"filters": {"atc4": ["C10A1"], "analysis_level": {"ubist": {"facility": []}}}, "source": "ubist", "measure": "sales"},
        {"filters": {"atc4": ["C10A1"], "analysis_level": {"ubist": {"specialty": [], "pairs": []}}}, "source": "ubist", "measure": "sales"},
    ]

    for payload in payloads:
        request = DynamicMarketRequest.model_validate(payload)
        assert request.filters.analysis_level.to_channel_axis(source=request.source) is None


def test_empty_iqvia_analysis_level_audit_code_payloads_normalize_like_missing_filter() -> None:
    payloads = [
        {"filters": {"atc4": ["C10A1"]}, "source": "iqvia", "measure": "sales"},
        {"filters": {"atc4": ["C10A1"], "analysis_level": {"iqvia": {}}}, "source": "iqvia", "measure": "sales"},
        {"filters": {"atc4": ["C10A1"], "analysis_level": {"iqvia": {"audit_code": []}}}, "source": "iqvia", "measure": "sales"},
    ]

    for payload in payloads:
        request = DynamicMarketRequest.model_validate(payload)
        assert request.filters.analysis_level.to_channel_axis(source=request.source) is None


def test_analysis_level_channel_slice_rejects_source_mismatch() -> None:
    request = DynamicMarketRequest.model_validate(
        {
            "filters": {
                "atc4": ["C10A1"],
                "analysis_level": {"iqvia": {"audit_code": ["KPA"]}},
            },
            "source": "ubist",
            "measure": "sales",
        }
    )

    try:
        request.filters.analysis_level.to_channel_axis(source=request.source)
    except ValueError as exc:
        assert "analysis_level.iqvia.audit_code must match selected source" in str(exc)
    else:
        raise AssertionError("source-mismatched analysis_level channel slice must be rejected")


def test_dynamic_market_request_rejects_removed_top_level_molecule_filter() -> None:
    with pytest.raises(Exception) as exc_info:
        DynamicMarketRequest.model_validate({"filters": {"molecule": ["PITAVASTATIN"]}})

    assert "molecule" in str(exc_info.value)


def test_dynamic_market_request_rejects_removed_metrics_option() -> None:
    with pytest.raises(Exception) as exc_info:
        DynamicMarketRequest.model_validate({"options": {"metrics": ["sales"]}})

    assert "metrics" in str(exc_info.value)


def test_dynamic_market_request_rejects_removed_top_n_option() -> None:
    with pytest.raises(ValidationError) as exc_info:
        DynamicMarketRequest.model_validate({"options": {"top_n": 10}})

    assert "top_n" in str(exc_info.value)


def test_dynamic_market_request_rejects_removed_analysis_fields() -> None:
    removed = {"ubist": ("class", "strength_pack", "ox_gx"), "iqvia": ("mfr", "nhi", "atc4")}
    for source, fields in removed.items():
        for field in fields:
            with pytest.raises(ValidationError) as exc_info:
                DynamicMarketRequest.model_validate({"filters": {"analysis_level": {source: {field: ["unused"]}}}})
            assert field in str(exc_info.value)


def test_general_dimension_payload_drops_iqvia_value_slice() -> None:
    request = DynamicMarketRequest.model_validate(
        {
            "source": "iqvia",
            "filters": {
                "atc4": ["A10C1"],
                "analysis_level": {"iqvia": {"audit_code": ["KPA"], "pack_desc": ["PACK"]}},
            },
        }
    )

    payload = request.filters.analysis_level.to_dimension_payload(source=request.source)["iqvia"]
    assert payload["pack_desc"] == ["PACK"]
    assert "audit_code" not in payload


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
    assert response["result"]["view"] == "general"
    assert response["result"]["market_meta"]["view"] == "general"
    assert response["result"]["market_meta"]["market_size_recent"] == 1.0


def test_route_uses_response_cache_for_general_dynamic_market(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeCache:
        def get_or_build(self, request, builder):
            captured["request"] = request
            captured["built"] = builder()
            return {"status": "SUCCESS", "result": {"cached": True}}

    monkeypatch.setattr(dynamic_market_route, "_dynamic_response_cache", FakeCache())
    monkeypatch.setattr(
        dynamic_market_route,
        "_build_general_dynamic_response",
        lambda _payload: {"status": "SUCCESS", "result": {"cached": False}},
    )

    response = dynamic_market_route.dynamic_market(
        DynamicMarketRequest.model_validate({"source": "ubist", "filters": {"atc4": ["C10A1"]}})
    )

    assert response == {"status": "SUCCESS", "result": {"cached": True}}
    assert captured["built"] == {"status": "SUCCESS", "result": {"cached": False}}
    assert captured["request"]["filters"]["atc4"] == ["C10A1"]


def test_route_returns_429_when_dynamic_miss_capacity_is_full(monkeypatch) -> None:
    class FullCache:
        def get_or_build(self, request, builder):
            raise dynamic_market_route.DynamicMarketOverloadedError("full")

    monkeypatch.setattr(dynamic_market_route, "_dynamic_response_cache", FullCache())

    with pytest.raises(dynamic_market_route.HTTPException) as exc_info:
        dynamic_market_route.dynamic_market(
            DynamicMarketRequest.model_validate({"source": "ubist", "filters": {"atc4": ["C10A1"]}})
        )

    assert exc_info.value.status_code == 429
    assert exc_info.value.detail["error"] == "dynamic_market_overloaded"


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
                    "analysis_level": {"ubist": {"facility": ["종합병원"]}},
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
                    "analysis_level": {"iqvia": {"audit_code": ["KPA"]}},
                },
            }
        )
    )

    assert response["status"] == "SUCCESS"
    assert captured["resolver_channel_axis"].source == "iqvia_nsa"
    assert captured["resolver_channel_axis"].audit_codes == ("KPA",)
    assert captured["aggregator_channel_axis"].audit_codes == ("KPA",)


def test_route_uses_cache_cause_builder_for_strategic_market(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_build_cached_payload(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {
            "data": {
                "ubist_specialty_channels": ["전체", "주요고객 종합병원 순환기", "의원 IGF"],
                "target_customer_competition_by_channel": {"주요고객 종합병원 순환기": {"views": []}},
            }
        }

    class FailingAggregator:
        def __init__(self, **_: object) -> None:
            raise AssertionError("strategic requests must not use the generic aggregator path")

    monkeypatch.setattr(dynamic_market_route, "get_strategic_payload", fake_build_cached_payload)
    monkeypatch.setattr(dynamic_market_route, "MetricAggregator", FailingAggregator)
    monkeypatch.setattr(
        "pipeline.scripts.api.dynamic_market.resolvers.db.fetch_all",
        lambda *_args, **_kwargs: [{"market_id": "ml_006"}],
    )

    response = dynamic_market_route.dynamic_market(
        DynamicMarketRequest.model_validate(
            {
                "filters": {
                    "view_kind": "market_landscape",
                    "focus_brand_key": "리바로",
                },
                "source": "ubist",
                "measure": "sales",
            }
        )
    )

    assert response["status"] == "SUCCESS"
    assert captured["ml_id"] == "ml_006"
    assert captured["focus_brand_key"] == "리바로"
    assert captured["cache"] is dynamic_market_route._dynamic_response_cache
    assert response["result"]["view"] == "strategic_ml"
    assert response["result"]["market_meta"]["view"] == "strategic_ml"
    assert response["result"]["data"]["ubist_specialty_channels"][1] == "주요고객 종합병원 순환기"


@pytest.mark.parametrize("view_kind", ["market_landscape", "competitive_dynamics"])
def test_route_forwards_period_range_to_every_strategic_view(monkeypatch, view_kind: str) -> None:
    captured: dict[str, object] = {}

    def fake_build_cached_payload(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"data": {"kpi": {}}}

    monkeypatch.setattr(dynamic_market_route, "get_strategic_payload", fake_build_cached_payload)
    monkeypatch.setattr(
        "pipeline.scripts.api.dynamic_market.resolvers.db.fetch_all",
        lambda *_args, **_kwargs: [{"market_id": "ml_006" if view_kind == "market_landscape" else "cd_006"}],
    )

    dynamic_market_route.dynamic_market(
        DynamicMarketRequest.model_validate(
            {
                "filters": {"view_kind": view_kind, "focus_brand_key": "리바로"},
                "source": "ubist",
                "measure": "sales",
                "options": {"period_range": {"start": "2025-01", "end": "2025-12"}},
            }
        )
    )

    assert captured["period_range"] == PeriodRange("2025-01", "2025-12")


def test_empty_period_response_is_null_with_explicit_reason() -> None:
    payload = DynamicMarketRequest.model_validate(
        {
            "filters": {"focus_brand_key": "리바로"},
            "source": "ubist",
            "measure": "sales",
            "options": {"period_range": {"start": "2030-01", "end": "2030-12"}},
        }
    )

    response = dynamic_market_route._empty_period_response(payload, "general")

    assert response["result"]["data"] is None
    assert response["result"]["reason"] == "no_data_in_period"
    assert response["result"]["period_range"] == {"start": "2030-01", "end": "2030-12"}


def test_route_accepts_explicit_strategic_ml_view_without_legacy_view_kind(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_build_cached_payload(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"view": "market_landscape", "market_meta": {"view": "market_landscape"}, "data": {"kpi": {}}}

    monkeypatch.setattr(dynamic_market_route, "get_strategic_payload", fake_build_cached_payload)
    monkeypatch.setattr(
        "pipeline.scripts.api.dynamic_market.resolvers.db.fetch_all",
        lambda *_args, **_kwargs: [{"market_id": "ml_006"}],
    )

    response = dynamic_market_route.dynamic_market(
        DynamicMarketRequest.model_validate(
            {
                "view": "strategic_ml",
                "filters": {"focus_brand_key": "리바로"},
                "source": "ubist",
                "measure": "sales",
            }
        )
    )

    assert response["status"] == "SUCCESS"
    assert captured["ml_id"] == "ml_006"
    assert response["result"]["view"] == "strategic_ml"
    assert response["result"]["market_meta"]["view"] == "strategic_ml"


def test_route_returns_explicit_empty_strategic_state_for_unmapped_mart_brand(monkeypatch) -> None:
    # Given: the focus brand exists in the general mart but has no strategic market membership.
    monkeypatch.setattr(
        "pipeline.scripts.api.dynamic_market.resolvers.db.fetch_all",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(dynamic_market_route, "_brand_exists", lambda _brand: True)

    # When: the brand is requested through the strategic dynamic route.
    response = dynamic_market_route.dynamic_market(
        DynamicMarketRequest.model_validate(
            {
                "view": "strategic_ml",
                "filters": {"focus_brand_key": "비JW브랜드"},
                "source": "ubist",
                "measure": "sales",
            }
        )
    )

    # Then: the route preserves the envelope and returns an explicit empty state, not 400/404.
    assert response["status"] == "SUCCESS"
    assert response["result"]["brand"] == "비JW브랜드"
    assert response["result"]["view"] == "strategic_ml"
    assert response["result"]["data"] is None
    assert response["result"]["reason"] == "brand_not_in_source"
    assert response["result"]["markets"] == []


def test_route_rejects_general_view_with_legacy_view_kind() -> None:
    try:
        dynamic_market_route.dynamic_market(
            DynamicMarketRequest.model_validate(
                {
                    "view": "general",
                    "filters": {"view_kind": "market_landscape", "atc4": ["C10A1"]},
                    "source": "ubist",
                    "measure": "sales",
                }
            )
        )
    except dynamic_market_route.HTTPException as exc:
        assert exc.status_code == 422
        assert "view=general cannot be combined" in str(exc.detail)
    else:
        raise AssertionError("view=general + view_kind must be rejected")


def test_route_rejects_conflicting_explicit_strategic_view_kind() -> None:
    try:
        dynamic_market_route.dynamic_market(
            DynamicMarketRequest.model_validate(
                {
                    "view": "strategic_ml",
                    "filters": {"view_kind": "competitive_dynamics", "focus_brand_key": "리바로"},
                    "source": "ubist",
                    "measure": "sales",
                }
            )
        )
    except dynamic_market_route.HTTPException as exc:
        assert exc.status_code == 422
        assert "conflicts" in str(exc.detail)
    else:
        raise AssertionError("conflicting strategic view must be rejected")


def test_route_rejects_analysis_level_channel_slice_for_strategic_view() -> None:
    try:
        dynamic_market_route.dynamic_market(
            DynamicMarketRequest.model_validate(
                {
                    "source": "iqvia",
                    "measure": "sales",
                    "filters": {
                        "view_kind": "market_landscape",
                        "focus_brand_key": "리바로",
                        "analysis_level": {"iqvia": {"audit_code": ["KPA"]}},
                    },
                }
            )
        )
    except dynamic_market_route.HTTPException as exc:
        assert exc.status_code == 400
        assert "analysis_level channel slice filters are supported only for general views" in str(exc.detail)
    else:
        raise AssertionError("strategic analysis_level channel slice must be rejected")


def test_route_rejects_analysis_level_filters_for_strategic_view() -> None:
    try:
        dynamic_market_route.dynamic_market(
            DynamicMarketRequest.model_validate(
                {
                    "source": "ubist",
                    "measure": "sales",
                    "filters": {
                        "view_kind": "market_landscape",
                        "focus_brand_key": "리바로",
                        "analysis_level": {"ubist": {"seller": ["JW중외제약"], "reimbursement": ["급여"]}},
                    },
                }
            )
        )
    except dynamic_market_route.HTTPException as exc:
        assert exc.status_code == 400
        assert "strategic view uses top-level filters.atc4" in str(exc.detail)
        assert "analysis_level.ubist.seller" in str(exc.detail)
        assert "analysis_level.ubist.reimbursement" in str(exc.detail)
    else:
        raise AssertionError("strategic analysis_level filters must be rejected")


def test_route_allows_top_level_atc4_for_strategic_view(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_build_cached_payload(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"data": {"kpi": {"market_size_recent": 1.0}}}

    monkeypatch.setattr(dynamic_market_route, "get_strategic_payload", fake_build_cached_payload)
    monkeypatch.setattr(
        "pipeline.scripts.api.dynamic_market.resolvers.db.fetch_all",
        lambda *_args, **_kwargs: [{"market_id": "ml_006"}],
    )

    response = dynamic_market_route.dynamic_market(
        DynamicMarketRequest.model_validate(
            {
                "source": "ubist",
                "measure": "sales",
                "filters": {
                    "view_kind": "market_landscape",
                    "focus_brand_key": "리바로",
                    "atc4": ["C10A1"],
                },
            }
        )
    )

    assert response["status"] == "SUCCESS"
    analysis_level = captured["analysis_level"]
    assert analysis_level.ubist.atc4 == ["C10A1"]


def test_route_rejects_strategic_analysis_level_atc_filters() -> None:
    try:
        dynamic_market_route.dynamic_market(
            DynamicMarketRequest.model_validate(
                {
                    "source": "ubist",
                    "measure": "sales",
                    "filters": {
                        "view_kind": "market_landscape",
                        "focus_brand_key": "리바로",
                        "analysis_level": {"ubist": {"atc3": ["C10A"], "atc4": ["C10A1"]}},
                    },
                }
            )
        )
    except dynamic_market_route.HTTPException as exc:
        assert exc.status_code == 400
        assert "strategic view uses top-level filters.atc4" in str(exc.detail)
        assert "analysis_level.ubist.atc3" in str(exc.detail)
        assert "analysis_level.ubist.atc4" in str(exc.detail)
    else:
        raise AssertionError("strategic analysis_level ATC filters must be rejected")


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
                "filters": {"atc4": ["C10A1"], "analysis_level": {"iqvia": {"audit_code": ["KPA"]}}},
            }
        ).filters.analysis_level.to_channel_axis(source="iqvia"),
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


def test_iqvia_pack_desc_filter_uses_pack_sidecar_dimension(monkeypatch) -> None:
    calls: list[tuple[str, tuple[str, ...]]] = []
    expected_hash = hashlib.sha256("PFS 162MG/0.9ML".encode("utf-8")).hexdigest()

    def fake_fetch_all(sql: str, params: tuple[str, ...]) -> list[dict]:
        calls.append((sql, params))
        if "mart_general_filter_dimension_metric" in sql:
            assert "dimension_type = %s" in sql
            assert "dimension_value_hash" in sql
            assert "pack" in params
            assert expected_hash in params
            return [
                {
                    "brand_key": "악템라",
                    "brand_name": "악템라",
                    "atc4_code": "M01C0",
                    "product_code": "pfs162",
                    "dimension_type": "pack",
                    "raw_value_history": json.dumps({"2026-Q1": 100.0}),
                }
            ]
        return [{"brand_key": "악템라", "atc4_code": "M01C0", "unit_label": "KRW"}]

    monkeypatch.setattr("pipeline.scripts.api.dynamic_market.aggregator.db.fetch_all", fake_fetch_all)
    metrics = MetricAggregator(mart_db="jw_mart").aggregate(
        brands=(BrandRef("악템라", "악템라", "M01C0"),),
        source="iqvia_nsa",
        measure="sales",
        period_range=PeriodRange(),
        top_n=20,
        dimension_filters=(DimensionFilter("pack", ("PFS 162MG/0.9ML",)),),
    )

    assert metrics.market_size == 100.0
    assert metrics.brands[0].brand_name == "악템라"
    assert any("mart_general_filter_dimension_metric" in sql for sql, _params in calls)


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


@pytest.mark.parametrize("field_name", ["ml_id", "cd_market_id"])
def test_dynamic_market_request_rejects_public_strategic_market_ids(field_name: str) -> None:
    with pytest.raises(ValidationError):
        DynamicMarketRequest.model_validate(
            {
                "filters": {
                    "view_kind": "market_landscape",
                    "focus_brand_key": "리바로",
                    field_name: "ml_006" if field_name == "ml_id" else "cd_006",
                }
            }
        )


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
    monkeypatch.setattr(
        "pipeline.scripts.api.dynamic_market.resolvers.db.fetch_all",
        lambda *_args, **_kwargs: [{"market_id": "ml_006"}],
    )
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


def test_dynamic_route_selects_first_ml_market_for_ambiguous_focus_brand(monkeypatch: pytest.MonkeyPatch) -> None:
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

    def fake_fetch_all(sql: str, params: tuple[str, ...]) -> list[dict]:
        assert "mart_strategic_ml_brand_metric" in sql
        assert "ORDER BY ml_id" in sql
        assert params == ("건카베딜", "건카베딜", "ubist", "sales")
        return [{"market_id": "ml_005"}, {"market_id": "ml_008"}]

    monkeypatch.setattr("pipeline.scripts.api.dynamic_market.resolvers.db.fetch_all", fake_fetch_all)
    payload = DynamicMarketRequest.model_validate(
        {
            "filters": {
                "view_kind": "market_landscape",
                "focus_brand_key": "건카베딜",
            },
            "source": "ubist",
            "measure": "sales",
        }
    )

    definition = dynamic_market_route._resolve_definition(payload)

    assert captured["ml_id"] == "ml_005"
    assert definition.strategic_market_id == "ml_005"


def test_dynamic_route_selects_first_cd_market_for_ambiguous_focus_brand(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class FakeStrategicResolver:
        def __init__(self, **_: object) -> None:
            pass

        def resolve(self, **kwargs: object) -> MarketDefinition:
            captured.update(kwargs)
            return MarketDefinition(
                view="strategic_cd",
                filter_echo={"view": "strategic_cd", "cd_market_id": kwargs["cd_market_id"]},
                source="ubist",
                measure="sales",
                strategic_market_kind="cd",
                strategic_market_id=str(kwargs["cd_market_id"]),
            )

    def fake_fetch_all(sql: str, params: tuple[str, ...]) -> list[dict]:
        assert "mart_strategic_cd_brand_metric" in sql
        assert "ORDER BY cd_market_id" in sql
        assert params == ("바로에젯", "바로에젯", "ubist", "sales")
        return [{"market_id": "cd_006"}, {"market_id": "cd_007"}]

    monkeypatch.setattr(dynamic_market_route, "StrategicViewResolver", FakeStrategicResolver)
    monkeypatch.setattr("pipeline.scripts.api.dynamic_market.resolvers.db.fetch_all", fake_fetch_all)
    payload = DynamicMarketRequest.model_validate(
        {
            "filters": {
                "view_kind": "competitive_dynamics",
                "focus_brand_key": "바로에젯",
            },
            "source": "ubist",
            "measure": "sales",
        }
    )

    definition = dynamic_market_route._resolve_definition(payload)

    assert captured["cd_market_id"] == "cd_006"
    assert definition.strategic_market_id == "cd_006"


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


def test_strategic_runtime_has_no_legacy_cache_cause_read() -> None:
    import inspect

    source = inspect.getsource(strategic_runtime)

    assert "FROM cache_cause" not in source
    assert "_cached_cause_payload" not in source


def test_strategic_runtime_clears_builder_caches_around_every_mart_build(monkeypatch) -> None:
    events: list[str] = []

    monkeypatch.setattr(strategic_runtime, "_clear_cause_builder_runtime_caches", lambda: events.append("clear"))
    monkeypatch.setattr(
        strategic_runtime,
        "_build_strategic_payload",
        lambda **_kwargs: events.append("build") or {"data": {"kpi": {}}},
    )

    result = strategic_runtime.build_strategic_payload(
        mart_db="mart",
        ml_id="ml_006",
        cd_market_id=None,
        focus_brand_key="리바로",
        source="ubist",
        measure="sales",
        analysis_level=DynamicMarketRequest().filters.analysis_level,
    )

    assert result == {"data": {"kpi": {}}}
    assert events == ["clear", "build", "clear"]


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


def test_strategic_sidecar_aggregation_hashes_atc_filters_like_sidecar(monkeypatch) -> None:
    calls: list[tuple[str, tuple[str, ...]]] = []
    expected_hash = hashlib.sha256("a10n1".encode("utf-8")).hexdigest()

    def fake_fetch_all(sql: str, params: tuple[str, ...]) -> list[dict]:
        calls.append((sql, params))
        if "mart_strategic_filter_dimension_metric" in sql:
            assert expected_hash in params
            assert hashlib.sha256("A10N1".encode("utf-8")).hexdigest() not in params
            return [
                {
                    "brand_key": "가드렛",
                    "brand_name": "가드렛",
                    "product_code": "644913980",
                    "dimension_type": "atc4",
                    "raw_value_history": json.dumps({"2026-04": 123.0}),
                }
            ]
        if "mart_strategic_ml_brand_metric" in sql:
            return [{"brand_key": "가드렛", "unit_label": "KRW"}]
        raise AssertionError(sql)

    monkeypatch.setattr("pipeline.scripts.api.dynamic_market.aggregator.db.fetch_all", fake_fetch_all)
    metrics = MetricAggregator(mart_db="jw_mart").aggregate(
        brands=(BrandRef("가드렛", "가드렛", ""),),
        source="ubist",
        measure="sales",
        period_range=PeriodRange("2026-04", "2026-04"),
        top_n=20,
        dimension_filters=(DimensionFilter("atc4", ("A10N1",)),),
        view="strategic_ml",
        strategic_market_id="ml_003",
    )

    assert metrics.market_size == 123.0
    assert metrics.brands[0].brand_name == "가드렛"
    assert any("mart_strategic_filter_dimension_metric" in sql for sql, _params in calls)
