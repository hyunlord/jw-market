from __future__ import annotations

from jw_chat_agent_poc.tools.query_layer.derived_models import (
    BrandKey,
    DerivedBrandPoint,
    DerivedMarketPoint,
)


def growth(start: float | None, end: float | None) -> float | None:
    return (float(end) / float(start) - 1) * 100 if start not in {None, 0} and end is not None else None


def compound_growth(
    start: float | None,
    end: float | None,
    elapsed_months: int | None,
    target_months: int,
) -> float | None:
    if start in {None, 0} or end is None or not elapsed_months:
        return None
    return ((float(end) / float(start)) ** (target_months / elapsed_months) - 1) * 100


def latest_brand_growth(
    brands: dict[BrandKey, DerivedBrandPoint],
    market: str,
    source: str,
    measure: str,
    brand: str,
    periods: tuple[str, ...],
    *,
    monthly: bool,
) -> float | None:
    if not periods or (monthly and not _is_monthly(periods[-1])):
        return None
    previous = _shift_year(periods[-1]) if not monthly else _shift_month(periods[-1])
    if previous not in periods:
        return None
    return growth(
        brands[(market, source, measure, brand, previous)].value_krw,
        brands[(market, source, measure, brand, periods[-1])].value_krw,
    )


def latest_market_growth(
    markets: dict[tuple[str, str, str, str], DerivedMarketPoint],
    market: str,
    source: str,
    measure: str,
    periods: tuple[str, ...],
    *,
    monthly: bool,
) -> float | None:
    if not periods or (monthly and not _is_monthly(periods[-1])):
        return None
    previous = _shift_year(periods[-1]) if not monthly else _shift_month(periods[-1])
    if previous not in periods:
        return None
    return growth(
        markets[(market, source, measure, previous)].total_krw,
        markets[(market, source, measure, periods[-1])].total_krw,
    )


def elapsed_months(start: str, end: str) -> int | None:
    if _is_monthly(start) and _is_monthly(end):
        return (int(end[:4]) - int(start[:4])) * 12 + int(end[5:7]) - int(start[5:7])
    if _is_quarterly(start) and _is_quarterly(end):
        quarters = (int(end[:4]) - int(start[:4])) * 4 + int(end[-1]) - int(start[-1])
        return quarters * 3
    return None


def terminal_streak(values: list[float]) -> tuple[str | None, int]:
    if len(values) < 2:
        return None, 0
    direction = "up" if values[-1] > values[-2] else "down" if values[-1] < values[-2] else None
    if direction is None:
        return None, 0
    count = 1
    for left, right in zip(reversed(values[:-1]), reversed(values[1:]), strict=True):
        if (direction == "up" and right > left) or (direction == "down" and right < left):
            count += 1
        else:
            break
    return direction, count


def turning_point(shares: list[tuple[str, float | None]]) -> tuple[str | None, str | None]:
    values = [(period, float(value)) for period, value in shares if value is not None]
    for index in range(1, len(values) - 1):
        previous, current, following = values[index - 1][1], values[index][1], values[index + 1][1]
        if current < previous and current < following:
            return values[index][0], "low"
        if current > previous and current > following:
            return values[index][0], "high"
    return None, None


def _shift_year(period: str) -> str:
    return f"{int(period[:4]) - 1}{period[4:]}" if _is_monthly(period) or _is_quarterly(period) else ""


def _shift_month(period: str) -> str:
    if not _is_monthly(period):
        return ""
    year, month = int(period[:4]), int(period[5:7])
    return f"{year - 1}-12" if month == 1 else f"{year:04d}-{month - 1:02d}"


def _is_monthly(period: str) -> bool:
    return len(period) == 7 and period[4] == "-" and period[5:7].isdigit()


def _is_quarterly(period: str) -> bool:
    return len(period) == 7 and period[4:6] == "-Q" and period[-1] in "1234"
