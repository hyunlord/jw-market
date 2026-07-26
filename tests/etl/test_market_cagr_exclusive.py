"""Contract: exclusive 5y/3y market CAGR + IQVIA quarter display format.

Guards the response-key spec (2026-07-21): the ``5y`` and ``3y`` CAGR slots are
mutually exclusive (never both non-null), ``None`` means "not computable" (never
coerced to 0), and IQVIA quarters render as ``YYYY-nQ``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "pipeline" / "scripts" / "etl"))

from cache_build_common import brand_cagr_exclusive, iqvia_period_to_display, market_cagr_exclusive


def _monthly(start_year: int, end_year: int, start_value: float, end_value: float) -> dict[str, float]:
    series = {f"{start_year}-05": start_value, f"{end_year}-05": end_value}
    return series


def test_five_year_endpoint_reports_5y_slot_only() -> None:
    series = _monthly(2021, 2026, 100.0, 200.0)
    cagr_5y, cagr_3y = market_cagr_exclusive(series)
    assert cagr_5y is not None
    assert cagr_3y is None  # ★ exclusive: 3y slot stays null when 5y is available


def test_three_year_endpoint_reports_3y_slot_only() -> None:
    series = _monthly(2023, 2026, 100.0, 200.0)
    cagr_5y, cagr_3y = market_cagr_exclusive(series)
    assert cagr_5y is None  # ★ no silent fallback into the 5y slot
    assert cagr_3y is not None


def test_neither_endpoint_reports_both_null() -> None:
    series = _monthly(2025, 2026, 100.0, 200.0)
    assert market_cagr_exclusive(series) == (None, None)


def test_null_is_never_zero() -> None:
    cagr_5y, cagr_3y = market_cagr_exclusive({"2025-05": 100.0, "2026-05": 200.0})
    assert cagr_5y is None and cagr_3y is None
    assert 0 not in (cagr_5y, cagr_3y)


@pytest.mark.parametrize(
    "series",
    [
        _monthly(2021, 2026, 100.0, 200.0),   # 5y capable
        _monthly(2023, 2026, 100.0, 150.0),   # 3y only
        _monthly(2025, 2026, 100.0, 120.0),   # neither
        {},                                    # empty
        {"2026-Q1": 100.0, "2021-Q1": 50.0},  # quarterly 5y
    ],
)
def test_exclusivity_invariant_never_both_non_null(series: dict[str, float]) -> None:
    # ★ failure injection: a silent 5y→3y fallback would set both slots.
    cagr_5y, cagr_3y = market_cagr_exclusive(series)
    assert not (cagr_5y is not None and cagr_3y is not None)


def test_brand_cagr_keeps_four_decimal_precision_and_exclusive_slots() -> None:
    five_year = _monthly(2021, 2026, 100.0, 121.0)
    three_year = _monthly(2023, 2026, 100.0, 90.0)

    expected_5y = round(((121.0 / 100.0) ** (1 / 5) - 1) * 100, 4)
    assert brand_cagr_exclusive(five_year) == (pytest.approx(expected_5y), None)
    assert brand_cagr_exclusive(three_year) == (None, pytest.approx(-3.4511))


def test_brand_iqvia_uses_19_quarters_but_never_18_for_five_year_slot() -> None:
    periods = _quarter_labels("2021-Q2", "2026-Q1")
    nineteen_quarters = {period: 100.0 + index for index, period in enumerate(periods)}
    eighteen_quarters = dict(list(nineteen_quarters.items())[1:])
    expected = round(((119.0 / 100.0) ** (1 / 4.75) - 1) * 100, 4)

    assert brand_cagr_exclusive(nineteen_quarters) == (pytest.approx(expected), None)
    assert brand_cagr_exclusive(eighteen_quarters)[0] is None


def test_brand_monthly_uses_59_month_span_for_five_year_window() -> None:
    months = {
        f"{2021 + (4 + offset) // 12:04d}-{(4 + offset) % 12 + 1:02d}": 100.0 + offset
        for offset in range(60)
    }
    expected = round(((159.0 / 100.0) ** (1 / (59 / 12)) - 1) * 100, 4)

    cagr_5y, cagr_3y = brand_cagr_exclusive(months)

    assert cagr_5y == pytest.approx(expected)
    assert cagr_3y is None


def _quarter_labels(start: str, end: str) -> tuple[str, ...]:
    year, quarter = (int(item) for item in start.replace("-Q", "-").split("-"))
    end_year, end_quarter = (int(item) for item in end.replace("-Q", "-").split("-"))
    result: list[str] = []
    while (year, quarter) <= (end_year, end_quarter):
        result.append(f"{year}-Q{quarter}")
        quarter += 1
        if quarter == 5:
            year += 1
            quarter = 1
    return tuple(result)


@pytest.mark.parametrize(
    ("mart_label", "expected"),
    [
        ("2026-Q1", "2026-1Q"),
        ("2026-Q4", "2026-4Q"),
        ("2025-Q2", "2025-2Q"),
        ("2026-05", None),   # monthly label is not a quarter
        (None, None),
        ("garbage", None),
    ],
)
def test_iqvia_period_to_display(mart_label: str | None, expected: str | None) -> None:
    assert iqvia_period_to_display(mart_label) == expected
