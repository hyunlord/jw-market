from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final
import re

from jw_chat_agent_poc.common.periods import canonical_periods


FIRST_AVAILABLE_PERIOD: Final[str] = "2021-01"
LATEST_AVAILABLE_PERIOD: Final[str] = "2026-04"
PERIOD_ALIASES: Final[tuple[str, ...]] = ("latest", "previous_year")


@dataclass(frozen=True, slots=True)
class AgentPeriodGrounding:
    available_months: tuple[str, ...]
    schema_periods: tuple[str, ...]
    pre_resolved_periods: tuple[str, ...]
    first_period: str
    latest_period: str

    def is_available(self, period: str) -> bool:
        return period in self.schema_periods


def build_period_grounding(
    question: str,
    current_month: Callable[[], str] | None = None,
    first_period: str = FIRST_AVAILABLE_PERIOD,
    latest_period: str = LATEST_AVAILABLE_PERIOD,
) -> AgentPeriodGrounding:
    available = _month_span(first_period, latest_period)
    pre_resolved = _pre_resolved_periods(question, current_month or _default_current_month, available, latest_period)
    return AgentPeriodGrounding(
        available_months=available,
        schema_periods=tuple(dict.fromkeys((*PERIOD_ALIASES, *available, *pre_resolved))),
        pre_resolved_periods=pre_resolved,
        first_period=first_period,
        latest_period=latest_period,
    )


def display_period(period: str | None, grounding: AgentPeriodGrounding) -> str:
    if period == "previous_year":
        return "2025"
    if period in {None, "", "latest"}:
        return grounding.latest_period
    return period


def require_available_period(period: str | None, grounding: AgentPeriodGrounding) -> str | None:
    if period in {None, ""}:
        return period
    if grounding.is_available(period):
        return period
    raise LookupError(
        f"Invalid period argument '{period}'. Use only available period enum "
        f"{grounding.first_period}~{grounding.latest_period} or aliases: {', '.join(PERIOD_ALIASES)}."
    )


def resolve_relative_expression(expression: str, current_month: str, grounding: AgentPeriodGrounding) -> str:
    period = _months_ago(expression, current_month)
    if period > grounding.latest_period:
        return grounding.latest_period
    if period < grounding.first_period:
        raise LookupError(f"Relative period '{expression}' resolves to {period}, outside {grounding.first_period}~{grounding.latest_period}.")
    return period


def _pre_resolved_periods(
    question: str,
    current_month: Callable[[], str],
    available: tuple[str, ...],
    latest_period: str,
) -> tuple[str, ...]:
    periods = list(_available_explicit_periods(canonical_periods(question), available))
    for match in re.finditer(r"\d{1,2}\s*(?:달|개월)\s*전", question):
        period = _months_ago(match.group(0), current_month())
        if period in available:
            periods.append(period)
    if any(token in question for token in ("최근", "최신", "현재")):
        periods.append("latest")
        periods.append(latest_period)
    if "작년" in question:
        periods.append("previous_year")
    return tuple(dict.fromkeys(periods))


def _available_explicit_periods(
    periods: tuple[str, ...],
    available_months: tuple[str, ...],
) -> tuple[str, ...]:
    if not available_months:
        return ()
    first_month, latest_month = available_months[0], available_months[-1]
    available: list[str] = []
    for period in periods:
        if "-Q" not in period:
            if period in available_months:
                available.append(period)
            continue
        year, quarter_text = period.split("-Q", 1)
        quarter = int(quarter_text)
        first_quarter_month = f"{year}-{(quarter - 1) * 3 + 1:02d}"
        last_quarter_month = f"{year}-{quarter * 3:02d}"
        if first_month <= first_quarter_month and last_quarter_month <= latest_month:
            available.append(period)
    return tuple(available)


def _month_span(start: str, end: str) -> tuple[str, ...]:
    months: list[str] = []
    current = start
    while current <= end:
        months.append(current)
        current = _shift_month(current, 1)
    return tuple(months)


def _months_ago(expression: str, current_month: str) -> str:
    match = re.search(r"(\d{1,2})\s*(?:달|개월)\s*전", expression)
    if not match:
        return current_month
    return _shift_month(current_month, -int(match.group(1)))


def _shift_month(month: str, delta: int) -> str:
    year = int(month[:4])
    month_num = int(month[5:7])
    index = year * 12 + month_num - 1 + delta
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def _default_current_month() -> str:
    from datetime import date

    today = date.today()
    return f"{today.year:04d}-{today.month:02d}"
