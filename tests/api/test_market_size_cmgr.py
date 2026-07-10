from __future__ import annotations

import pytest

from pipeline.scripts.api.composers.cache_to_response import compose_cached_json
from pipeline.scripts.api.dynamic_market.cause_time import market_size_series
from pipeline.scripts.api.dynamic_market.types import AggregatedMetrics
from pipeline.scripts.api.market_scope.legacy_shape import market_size_series_payload
from pipeline.scripts.etl.build_cache_cause import market_size_series_with_yoy


@pytest.mark.parametrize(
    ("source", "periods", "expected_periods_per_year"),
    [
        ("ubist", ("2025-01", "2026-01"), 12),
        ("iqvia_nsa", ("2025-Q1", "2026-Q1"), 4),
    ],
)
def test_dynamic_market_size_series_adds_source_period_cmgr(
    source: str,
    periods: tuple[str, str],
    expected_periods_per_year: int,
) -> None:
    metrics = _metrics(source=source, periods=periods, values=(100.0, 121.0))

    points = market_size_series(metrics)

    expected = ((121.0 / 100.0) ** (1 / expected_periods_per_year) - 1) * 100
    assert points[0]["mom_growth_pct"] is None
    assert points[1]["mom_growth_pct"] == pytest.approx(expected)


def test_dynamic_market_size_series_preserves_existing_point_values() -> None:
    metrics = _metrics(source="ubist", periods=("2025-01", "2026-01"), values=(100.0, 121.0))

    point = market_size_series(metrics)[1]

    without_cmgr = {key: value for key, value in point.items() if key != "mom_growth_pct"}
    assert without_cmgr == {
        "period": "2026-01",
        "value": 121.0,
        "yoy_growth_pct": 21.0,
        "sales_krw": 121.0,
    }


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ((0.0, 121.0), None),
        ((100.0, 0.0), -100.0),
    ],
)
def test_dynamic_market_size_series_handles_cmgr_zero_boundaries(
    values: tuple[float, float],
    expected: float | None,
) -> None:
    metrics = _metrics(source="ubist", periods=("2025-01", "2026-01"), values=values)

    points = market_size_series(metrics)

    assert points[1]["mom_growth_pct"] == expected


def test_dynamic_market_size_series_returns_null_without_exact_prior_year() -> None:
    metrics = _metrics(source="ubist", periods=("2025-02", "2026-01"), values=(100.0, 121.0))

    points = market_size_series(metrics)

    assert points[1]["mom_growth_pct"] is None


@pytest.mark.parametrize(
    ("source", "periods", "expected_periods_per_year"),
    [
        ("UBIST", ("2025-01", "2026-01"), 12),
        ("IQVIA", ("2025-Q1", "2026-Q1"), 4),
    ],
)
def test_scoped_market_size_payload_matches_dynamic_cmgr_definition(
    source: str,
    periods: tuple[str, str],
    expected_periods_per_year: int,
) -> None:
    market_size = {periods[0]: 100.0, periods[1]: 121.0}

    points = market_size_series_payload(market_size, source=source)

    expected = ((121.0 / 100.0) ** (1 / expected_periods_per_year) - 1) * 100
    assert points[periods[0]]["mom_growth_pct"] is None
    assert points[periods[1]]["mom_growth_pct"] == pytest.approx(expected)


@pytest.mark.parametrize(
    ("periods", "expected_periods_per_year"),
    [
        (("2025-01", "2026-01"), 12),
        (("2025-Q1", "2026-Q1"), 4),
    ],
)
def test_static_market_size_payload_adds_rounded_cmgr_without_changing_old_keys(
    periods: tuple[str, str],
    expected_periods_per_year: int,
) -> None:
    market_size = {periods[0]: 100.0, periods[1]: 121.0}

    points = market_size_series_with_yoy(market_size)

    expected = round(((121.0 / 100.0) ** (1 / expected_periods_per_year) - 1) * 100, 4)
    assert points[periods[0]]["mom_growth_pct"] is None
    assert points[periods[1]]["mom_growth_pct"] == expected
    assert {key: value for key, value in points[periods[1]].items() if key != "mom_growth_pct"} == {
        "value": 121.0,
        "yoy_growth_pct": None,
    }


@pytest.mark.parametrize(
    ("source", "periods", "expected_periods_per_year"),
    [
        ("UBIST", ("2025-01", "2026-01"), 12),
        ("IQVIA", ("2025-Q1", "2026-Q1"), 4),
    ],
)
def test_cached_cause_response_adds_cmgr_without_rebuilding_cache(
    source: str,
    periods: tuple[str, str],
    expected_periods_per_year: int,
) -> None:
    cached = {
        "data": {
            "market_size_series": {
                periods[0]: {"value": 100.0, "yoy_growth_pct": None},
                periods[1]: {"value": 121.0, "yoy_growth_pct": 21.0},
            }
        }
    }

    payload = compose_cached_json(cached, measure="sales", source=source)

    points = payload["data"]["market_size_series"]
    expected = ((121.0 / 100.0) ** (1 / expected_periods_per_year) - 1) * 100
    assert points[0]["mom_growth_pct"] is None
    assert points[1]["mom_growth_pct"] == pytest.approx(expected, abs=0.0001)
    assert {key: value for key, value in points[1].items() if key != "mom_growth_pct"} == {
        "period": periods[1],
        "value": 121.0,
        "yoy_growth_pct": 21.0,
        "sales_krw": 121.0,
    }


def _metrics(
    *,
    source: str,
    periods: tuple[str, str],
    values: tuple[float, float],
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
