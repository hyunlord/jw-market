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

from cache_build_common import iqvia_period_to_display, market_cagr_exclusive


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
