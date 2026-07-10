"""Growth calculations shared by cause response paths."""

from __future__ import annotations

import math


def compound_period_growth_pct(
    previous: float | None,
    current: float | None,
    periods_per_year: int,
) -> float | None:
    """Return the compound period growth versus the prior-year period."""

    if previous is None or previous <= 0 or current is None or current < 0:
        return None
    return (math.pow(current / previous, 1 / periods_per_year) - 1) * 100
