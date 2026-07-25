"""Market-share momentum formulas shared across pipeline layers."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence


def compute_momentum(market_share_percent: Sequence[float | None]) -> float | None:
    """Return the OLS slope over the latest four market-share points."""

    if len(market_share_percent) < 4:
        return None
    recent = market_share_percent[-4:]
    if any(value is None or not math.isfinite(float(value)) for value in recent):
        return None
    ys = [float(value) for value in recent if value is not None]
    xs = (1, 2, 3, 4)
    sum_xy = sum(x * y for x, y in zip(xs, ys, strict=True))
    return (4 * sum_xy - 10 * sum(ys)) / 20


def compute_market_share_momentum(
    brand_value_history: Mapping[str, float],
    market_value_history: Mapping[str, float],
) -> float | None:
    """Build market-share history and return its latest-four-point OLS slope."""

    market_share_percent = [
        (
            float(brand_value_history.get(period, 0.0)) / float(market_total) * 100.0
            if float(market_total) > 0
            else 0.0
        )
        for period, market_total in sorted(market_value_history.items())
    ]
    return compute_momentum(market_share_percent)
