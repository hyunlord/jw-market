from __future__ import annotations

from decimal import Decimal
import json

import pytest

from pipeline.scripts.api.composers.cache_to_response import compose_cached_json
from pipeline.scripts.api.dynamic_market.aggregator import (
    MetricAggregator,
    parse_history,
    sidecar_rows_to_metric_rows,
    strategic_sidecar_rows_to_metric_rows,
)
from pipeline.scripts.api.dynamic_market.cause_payload import build_market_meta
from pipeline.scripts.api.dynamic_market.cause_time import market_size_series
from pipeline.scripts.api.dynamic_market.response_cache import CACHE_SCHEMA_VERSION
from pipeline.scripts.api.dynamic_market.types import AggregatedMetrics, MarketDefinition, PeriodRange
from pipeline.scripts.api.market_growth import fixed_five_year_growth_series, growth_endpoint_meta
from pipeline.scripts.api.market_scope.legacy_shape import market_size_series_payload
from pipeline.scripts.etl.build_cache_cause import market_size_series_with_yoy


def test_ubist_growth_keeps_one_range_baseline_and_uses_elapsed_months() -> None:
    series = {
        "2021-06": 90_209_049_371.0,
        "2025-09": 85_000_000_000.0,
        "2026-02": 82_054_035_370.0,
        "2026-05": 87_019_172_843.0,
    }

    results = fixed_five_year_growth_series(series, source="ubist")

    assert {result.baseline_period for result in results.values()} == {"2021-06"}
    assert {period: result.period_count for period, result in results.items()} == {
        "2021-06": 0,
        "2025-09": 51,
        "2026-02": 56,
        "2026-05": 59,
    }
    expected = (
        (Decimal("82054035370") / Decimal("90209049371"))
        ** (Decimal(12) / Decimal(56))
        - Decimal(1)
    ) * Decimal(100)
    assert results["2026-02"].value == pytest.approx(float(expected), abs=0.0001)
    assert results["2026-02"].value < 0


def test_iqvia_growth_keeps_one_range_baseline_and_uses_elapsed_quarters() -> None:
    series = {
        "2021-Q2": 100.0,
        "2021-Q3": 102.0,
        "2025-Q4": 118.0,
        "2026-Q1": 121.0,
    }

    results = fixed_five_year_growth_series(series, source="iqvia_nsa")

    assert {result.baseline_period for result in results.values()} == {"2021-Q2"}
    assert {period: result.period_count for period, result in results.items()} == {
        "2021-Q2": 0,
        "2021-Q3": 1,
        "2025-Q4": 18,
        "2026-Q1": 19,
    }
    expected = ((121.0 / 100.0) ** (4 / 19) - 1) * 100
    assert results["2026-Q1"].value == pytest.approx(expected)


def test_growth_uses_latest_five_year_period_when_present_in_range() -> None:
    results = fixed_five_year_growth_series(
        {"2021-05": 100.0, "2022-05": 105.0, "2026-05": 121.0},
        source="ubist",
    )

    assert {result.baseline_period for result in results.values()} == {"2021-05"}


@pytest.mark.parametrize(
    ("source", "periods"),
    [
        (
            "ubist",
            tuple(f"{2021 + (index + 5) // 12:04d}-{(index + 5) % 12 + 1:02d}" for index in range(60)),
        ),
        (
            "iqvia_nsa",
            tuple(f"{2021 + (index + 1) // 4:04d}-Q{(index + 1) % 4 + 1}" for index in range(20)),
        ),
    ],
)
def test_range_baseline_is_stable_for_every_response_period(source: str, periods: tuple[str, ...]) -> None:
    results = fixed_five_year_growth_series(
        {period: float(100 + index) for index, period in enumerate(periods)},
        source=source,
    )

    assert len(results) == len(periods)
    assert {result.baseline_period for result in results.values()} == {periods[0]}


@pytest.mark.parametrize(
    ("source", "periods", "periods_per_year", "elapsed_periods"),
    [
        ("ubist", ("2021-05", "2026-05"), 12, 60),
        ("iqvia_nsa", ("2021-Q1", "2026-Q1"), 4, 20),
    ],
)
def test_dynamic_market_size_series_uses_range_baseline_growth(
    source: str,
    periods: tuple[str, str],
    periods_per_year: int,
    elapsed_periods: int,
) -> None:
    metrics = _metrics(source=source, periods=periods, values=(100.0, 121.0))

    points = market_size_series(metrics)

    expected = ((121.0 / 100.0) ** (periods_per_year / elapsed_periods) - 1) * 100
    assert points[0]["mom_growth_pct"] is None
    assert points[1]["mom_growth_pct"] == pytest.approx(expected)


def test_dynamic_market_size_series_preserves_existing_point_values() -> None:
    metrics = _metrics(source="ubist", periods=("2021-05", "2026-05"), values=(100.0, 121.0))

    point = market_size_series(metrics)[1]

    without_growth = {key: value for key, value in point.items() if key != "mom_growth_pct"}
    assert without_growth == {
        "period": "2026-05",
        "value": 121.0,
        "yoy_growth_pct": None,
        "sales_krw": 121.0,
    }


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ((0.0, 121.0), None),
        ((100.0, 0.0), -100.0),
    ],
)
def test_dynamic_market_size_series_handles_growth_boundaries(
    values: tuple[float, float],
    expected: float | None,
) -> None:
    metrics = _metrics(source="ubist", periods=("2021-05", "2026-05"), values=values)

    points = market_size_series(metrics)

    assert points[1]["mom_growth_pct"] == expected


def test_dynamic_market_size_series_uses_earliest_value_and_actual_elapsed_months() -> None:
    metrics = _metrics(source="ubist", periods=("2023-01", "2026-05"), values=(100.0, 121.0))

    points = market_size_series(metrics)

    assert points[1]["mom_growth_pct"] == pytest.approx(((121.0 / 100.0) ** (12 / 40) - 1) * 100)


@pytest.mark.parametrize(
    ("series", "reason"),
    [
        ({"2026-05": 121.0}, "insufficient_history"),
        ({"2021-05": 0.0, "2026-05": 121.0}, "zero_baseline"),
        ({"2021-05": -1.0, "2026-05": 121.0}, "invalid_baseline"),
    ],
)
def test_growth_result_distinguishes_unavailable_reasons(
    series: dict[str, float],
    reason: str,
) -> None:
    latest = sorted(series)[-1]

    result = fixed_five_year_growth_series(series, source="ubist")[latest]

    assert result.value is None
    assert result.reason == reason


def test_growth_skips_missing_baseline_period_but_preserves_actual_zero() -> None:
    missing = fixed_five_year_growth_series(
        {"2021-05": None, "2022-05": 100.0, "2026-05": 121.0},
        source="ubist",
    )["2026-05"]
    actual_zero = fixed_five_year_growth_series(
        {"2021-05": 0.0, "2022-05": 100.0, "2026-05": 121.0},
        source="ubist",
    )["2026-05"]

    assert missing.baseline_period == "2022-05"
    assert missing.value == pytest.approx(((121.0 / 100.0) ** (12 / 48) - 1) * 100)
    assert actual_zero.reason == "zero_baseline"
    assert actual_zero.value is None


def test_growth_does_not_turn_missing_endpoint_into_minus_one_hundred_percent() -> None:
    result = fixed_five_year_growth_series(
        {"2021-05": 100.0, "2026-05": None},
        source="ubist",
    )["2026-05"]

    assert result.value is None
    assert result.reason == "insufficient_history"


def test_parse_history_preserves_null_separately_from_actual_zero() -> None:
    assert parse_history('{"2026-04": 0, "2026-05": null}') == {
        "2026-04": 0.0,
        "2026-05": None,
    }


def test_aggregation_excludes_incomplete_period_instead_of_summing_missing_as_zero() -> None:
    rows = [
        {
            "brand_key": "a",
            "brand_name": "A",
            "atc4_code": "A10N1",
            "unit_label": "KRW",
            "raw_value_history": '{"2026-04": 10, "2026-05": null}',
        },
        {
            "brand_key": "b",
            "brand_name": "B",
            "atc4_code": "A10N1",
            "unit_label": "KRW",
            "raw_value_history": '{"2026-04": 20, "2026-05": 30}',
        },
    ]

    brands, totals = MetricAggregator(mart_db="jw_mart")._aggregate_rows(
        rows,
        period_range=PeriodRange(),
    )

    assert totals == {"2026-04": 30.0}
    assert all(brand.latest_period == "2026-04" for brand in brands)
    assert all("2026-05" not in brand.history_by_period for brand in brands)


@pytest.mark.parametrize("strategic", [False, True])
def test_sidecar_collapse_propagates_incomplete_period(strategic: bool) -> None:
    rows = [
        {
            "brand_key": "a",
            "brand_name": "A",
            "atc4_code": "A10N1",
            "product_code": "P1",
            "dimension_type": "molecule",
            "raw_value_history": '{"2026-04": 10, "2026-05": null}',
        },
        {
            "brand_key": "a",
            "brand_name": "A",
            "atc4_code": "A10N1",
            "product_code": "P2",
            "dimension_type": "molecule",
            "raw_value_history": '{"2026-04": 20, "2026-05": 30}',
        },
    ]

    if strategic:
        collapsed = strategic_sidecar_rows_to_metric_rows(
            rows,
            metadata={},
            required_dimensions=("molecule",),
        )
    else:
        collapsed = sidecar_rows_to_metric_rows(
            rows,
            metadata={},
            required_dimensions=("molecule",),
        )

    assert json.loads(collapsed[0]["raw_value_history"]) == {
        "2026-04": 30.0,
        "2026-05": None,
    }


def test_market_meta_discloses_latest_available_growth_endpoint() -> None:
    metrics = _metrics(source="ubist", periods=("2021-04", "2026-04"), values=(100.0, 121.0))
    definition = MarketDefinition(
        view="general",
        filter_echo={"atc4": ["A10N1"]},
        source="ubist",
        measure="sales",
    )

    meta = build_market_meta(
        definition=definition,
        metrics=metrics,
        market_id="dynamic_general_test",
        data={"market_size_series": market_size_series(metrics)},
    )

    assert meta["mom_growth_meta"] == {
        "end_period": "2026-04",
        "reason": "latest_available",
    }


def test_growth_endpoint_meta_ignores_missing_values_but_preserves_actual_zero() -> None:
    assert growth_endpoint_meta({"2026-03": 1.0, "2026-04": 0.0, "2026-05": None}) == {
        "end_period": "2026-04",
        "reason": "latest_available",
    }


@pytest.mark.parametrize(
    ("source", "periods", "periods_per_year", "elapsed_periods"),
    [
        ("UBIST", ("2021-05", "2026-05"), 12, 60),
        ("IQVIA", ("2021-Q1", "2026-Q1"), 4, 20),
    ],
)
def test_scoped_market_size_payload_matches_dynamic_growth_definition(
    source: str,
    periods: tuple[str, str],
    periods_per_year: int,
    elapsed_periods: int,
) -> None:
    market_size = {periods[0]: 100.0, periods[1]: 121.0}

    points = market_size_series_payload(market_size, source=source)

    expected = ((121.0 / 100.0) ** (periods_per_year / elapsed_periods) - 1) * 100
    assert points[periods[0]]["mom_growth_pct"] is None
    assert points[periods[1]]["mom_growth_pct"] == pytest.approx(expected)


@pytest.mark.parametrize(
    ("periods", "periods_per_year", "elapsed_periods"),
    [
        (("2021-05", "2026-05"), 12, 60),
        (("2021-Q1", "2026-Q1"), 4, 20),
    ],
)
def test_static_market_size_payload_adds_rounded_growth_without_changing_old_keys(
    periods: tuple[str, str],
    periods_per_year: int,
    elapsed_periods: int,
) -> None:
    market_size = {periods[0]: 100.0, periods[1]: 121.0}

    points = market_size_series_with_yoy(market_size)

    expected = round(((121.0 / 100.0) ** (periods_per_year / elapsed_periods) - 1) * 100, 4)
    assert points[periods[0]]["mom_growth_pct"] is None
    assert points[periods[1]]["mom_growth_pct"] == expected
    assert {key: value for key, value in points[periods[1]].items() if key != "mom_growth_pct"} == {
        "value": 121.0,
        "yoy_growth_pct": None,
    }


@pytest.mark.parametrize(
    ("source", "periods", "periods_per_year", "elapsed_periods"),
    [
        ("UBIST", ("2021-05", "2026-05"), 12, 60),
        ("IQVIA", ("2021-Q1", "2026-Q1"), 4, 20),
    ],
)
def test_cached_cause_response_recomputes_growth_without_rebuilding_cache(
    source: str,
    periods: tuple[str, str],
    periods_per_year: int,
    elapsed_periods: int,
) -> None:
    cached = {
        "market_meta": {},
        "data": {
            "market_size_series": {
                periods[0]: {"value": 100.0, "yoy_growth_pct": None, "mom_growth_pct": 999.0},
                periods[1]: {"value": 121.0, "yoy_growth_pct": 21.0, "mom_growth_pct": 999.0},
            }
        }
    }

    payload = compose_cached_json(cached, measure="sales", source=source)

    points = payload["data"]["market_size_series"]
    assert payload["market_meta"]["mom_growth_meta"] == {
        "end_period": periods[1],
        "reason": "latest_available",
    }
    expected = ((121.0 / 100.0) ** (periods_per_year / elapsed_periods) - 1) * 100
    assert points[0]["mom_growth_pct"] is None
    assert points[1]["mom_growth_pct"] == pytest.approx(expected, abs=0.0001)
    assert {key: value for key, value in points[1].items() if key != "mom_growth_pct"} == {
        "period": periods[1],
        "value": 121.0,
        "yoy_growth_pct": 21.0,
        "sales_krw": 121.0,
    }


@pytest.mark.parametrize("value_key", ["raw_value", "market_size"])
def test_cached_cause_response_recomputes_growth_from_legacy_value_aliases(value_key: str) -> None:
    cached = {
        "data": {
            "market_size_series": {
                "2021-05": {value_key: 100.0, "mom_growth_pct": 999.0},
                "2026-05": {value_key: 121.0, "mom_growth_pct": 999.0},
            }
        }
    }

    payload = compose_cached_json(cached, measure="sales", source="UBIST")

    expected = ((121.0 / 100.0) ** (12 / 60) - 1) * 100
    assert payload["data"]["market_size_series"][1]["mom_growth_pct"] == pytest.approx(expected, abs=0.0001)


def test_dynamic_output_changes_invalidate_legacy_response_cache() -> None:
    assert CACHE_SCHEMA_VERSION == "dynamic-market-response-v6-contiguous-rankings-range-baseline"


def _metrics(
    *,
    source: str,
    periods: tuple[str, ...],
    values: tuple[float, ...],
) -> AggregatedMetrics:
    return AggregatedMetrics(
        source=source,
        measure="sales",
        unit_label="KRW",
        market_size=sum(values),
        hhi=None,
        cagr=None,
        monthly_series=tuple(
            {"period": period, "market_size": value}
            for period, value in zip(periods, values, strict=True)
        ),
        brands=(),
    )
