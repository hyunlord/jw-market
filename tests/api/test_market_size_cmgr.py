from __future__ import annotations

import pytest

from pipeline.scripts.api.composers.cache_to_response import compose_cached_json
from pipeline.scripts.api.dynamic_market.cause_time import market_size_series
from pipeline.scripts.api.dynamic_market.response_cache import CACHE_SCHEMA_VERSION
from pipeline.scripts.api.dynamic_market.types import AggregatedMetrics
from pipeline.scripts.api.market_growth import fixed_five_year_growth_series
from pipeline.scripts.api.market_scope.legacy_shape import market_size_series_payload
from pipeline.scripts.etl.build_cache_cause import market_size_series_with_yoy


@pytest.mark.parametrize(
    ("source", "periods", "fixed_period_count"),
    [
        ("ubist", ("2021-05", "2026-05"), 60),
        ("iqvia_nsa", ("2021-Q1", "2026-Q1"), 20),
    ],
)
def test_dynamic_market_size_series_uses_fixed_five_year_growth(
    source: str,
    periods: tuple[str, str],
    fixed_period_count: int,
) -> None:
    metrics = _metrics(source=source, periods=periods, values=(100.0, 121.0))

    points = market_size_series(metrics)

    expected = ((121.0 / 100.0) ** (1 / fixed_period_count) - 1) * 100
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


def test_dynamic_market_size_series_uses_earliest_value_but_keeps_fixed_exponent() -> None:
    metrics = _metrics(source="ubist", periods=("2023-01", "2026-05"), values=(100.0, 121.0))

    points = market_size_series(metrics)

    assert points[1]["mom_growth_pct"] == pytest.approx(((121.0 / 100.0) ** (1 / 60) - 1) * 100)


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


@pytest.mark.parametrize(
    ("source", "periods", "fixed_period_count"),
    [
        ("UBIST", ("2021-05", "2026-05"), 60),
        ("IQVIA", ("2021-Q1", "2026-Q1"), 20),
    ],
)
def test_scoped_market_size_payload_matches_dynamic_growth_definition(
    source: str,
    periods: tuple[str, str],
    fixed_period_count: int,
) -> None:
    market_size = {periods[0]: 100.0, periods[1]: 121.0}

    points = market_size_series_payload(market_size, source=source)

    expected = ((121.0 / 100.0) ** (1 / fixed_period_count) - 1) * 100
    assert points[periods[0]]["mom_growth_pct"] is None
    assert points[periods[1]]["mom_growth_pct"] == pytest.approx(expected)


@pytest.mark.parametrize(
    ("periods", "fixed_period_count"),
    [
        (("2021-05", "2026-05"), 60),
        (("2021-Q1", "2026-Q1"), 20),
    ],
)
def test_static_market_size_payload_adds_rounded_growth_without_changing_old_keys(
    periods: tuple[str, str],
    fixed_period_count: int,
) -> None:
    market_size = {periods[0]: 100.0, periods[1]: 121.0}

    points = market_size_series_with_yoy(market_size)

    expected = round(((121.0 / 100.0) ** (1 / fixed_period_count) - 1) * 100, 4)
    assert points[periods[0]]["mom_growth_pct"] is None
    assert points[periods[1]]["mom_growth_pct"] == expected
    assert {key: value for key, value in points[periods[1]].items() if key != "mom_growth_pct"} == {
        "value": 121.0,
        "yoy_growth_pct": None,
    }


@pytest.mark.parametrize(
    ("source", "periods", "fixed_period_count"),
    [
        ("UBIST", ("2021-05", "2026-05"), 60),
        ("IQVIA", ("2021-Q1", "2026-Q1"), 20),
    ],
)
def test_cached_cause_response_recomputes_growth_without_rebuilding_cache(
    source: str,
    periods: tuple[str, str],
    fixed_period_count: int,
) -> None:
    cached = {
        "data": {
            "market_size_series": {
                periods[0]: {"value": 100.0, "yoy_growth_pct": None, "mom_growth_pct": 999.0},
                periods[1]: {"value": 121.0, "yoy_growth_pct": 21.0, "mom_growth_pct": 999.0},
            }
        }
    }

    payload = compose_cached_json(cached, measure="sales", source=source)

    points = payload["data"]["market_size_series"]
    expected = ((121.0 / 100.0) ** (1 / fixed_period_count) - 1) * 100
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

    expected = ((121.0 / 100.0) ** (1 / 60) - 1) * 100
    assert payload["data"]["market_size_series"][1]["mom_growth_pct"] == pytest.approx(expected, abs=0.0001)


def test_fixed_five_year_growth_invalidates_legacy_dynamic_response_cache() -> None:
    assert CACHE_SCHEMA_VERSION == "dynamic-market-response-v3-fixed-five-year-growth"


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
