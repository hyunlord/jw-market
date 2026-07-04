from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import re
from typing import Final

from jw_chat_agent_poc.agentic.news_filters import FilterValue, UnsupportedFilter
from jw_chat_agent_poc.agentic.sales_filters import MetricFilterPlan


_MONTH_RE: Final[re.Pattern[str]] = re.compile(r"20\d{2}-(0[1-9]|1[0-2])")
_DAILY_TOKENS: Final[frozenset[str]] = frozenset({"오늘", "어제", "하루전", "1일전"})


@dataclass(frozen=True, slots=True)
class RelativePeriodResolution:
    months: tuple[str, ...] = ()
    label: str = ""
    applied_filters: dict[str, FilterValue] = field(default_factory=dict)
    interpretation_notes: tuple[dict[str, str], ...] = ()
    unsupported: tuple[UnsupportedFilter, ...] = ()
    data_basis: dict[str, str] = field(default_factory=dict)


def resolve_relative_periods(plan: MetricFilterPlan, periods: tuple[str, ...]) -> RelativePeriodResolution | None:
    if plan.relative_period is None and plan.relative_range is None:
        return None
    months = _available_months(periods)
    if not months:
        return _unsupported(plan.relative_period or plan.relative_range or "", "월 단위 cache 기간을 찾지 못했습니다.", "-", "-")
    first = months[0]
    latest = months[-1]
    current = _current_month()
    if plan.relative_period is not None:
        return _resolve_relative_period(plan.relative_period, first, latest, current)
    if plan.relative_range is not None:
        return _resolve_relative_range(plan.relative_range, first, latest, current)
    return None


def _current_month() -> str:
    return date.today().strftime("%Y-%m")


def _resolve_relative_period(value: str, first: str, latest: str, current: str) -> RelativePeriodResolution:
    if value in _DAILY_TOKENS:
        reason = f"매출 cache는 월 단위입니다. 요청하신 {current} 데이터는 아직 없습니다. 최신은 {latest}까지입니다."
        return _unsupported(value, reason, first, latest)
    if value == "이번달":
        return _single_month(value, current, first, latest, _current_basis(current))
    month_delta = _relative_month_delta(value)
    if month_delta is not None:
        return _single_month(value, _shift_month(current, -month_delta), first, latest, _current_basis(current))
    year_delta = _relative_year_delta(value)
    if year_delta is not None:
        return _single_month(value, _shift_month(current, -year_delta * 12), first, latest, _current_basis(current))
    return _unsupported(value, f"상대 날짜 표현을 해석하지 못했습니다. 데이터는 {first}~{latest}까지 있습니다.", first, latest)


def _resolve_relative_range(value: str, first: str, latest: str, current: str) -> RelativePeriodResolution:
    months = _recent_month_count(value)
    if months is None:
        return _unsupported(value, f"상대 기간 표현을 해석하지 못했습니다. 데이터는 {first}~{latest}까지 있습니다.", first, latest)
    requested_end = _shift_month(current, -1)
    requested_start = _shift_month(requested_end, -(months - 1))
    if requested_end < first or requested_start > latest:
        reason = f"요청 구간 {requested_start}~{requested_end}은 cache 범위 밖입니다. 데이터는 {first}~{latest}까지 있습니다."
        return _unsupported(value, reason, first, latest)
    start = max(requested_start, first)
    end = min(requested_end, latest)
    label = f"{start}~{end}"
    return RelativePeriodResolution(
        months=_month_span(start, end),
        label=label,
        applied_filters={"period_range": label},
        interpretation_notes=(
            {
                "requested": value,
                "interpreted_as": label,
                "basis": _range_basis(current, requested_start, requested_end, latest, start, end),
            },
        ),
        data_basis=_data_basis(first, latest),
    )


def _single_month(requested: str, month: str, first: str, latest: str, basis: str) -> RelativePeriodResolution:
    if month > latest:
        reason = f"요청하신 {month} 데이터는 아직 없습니다. 최신은 {latest}까지입니다."
        return _unsupported(requested, reason, first, latest)
    if month < first:
        reason = f"요청 기간 {month}은 cache 범위 밖입니다. 데이터는 {first}~{latest}까지 있습니다."
        return _unsupported(requested, reason, first, latest)
    return RelativePeriodResolution(
        months=(month,),
        label=month,
        applied_filters={"period_month": month},
        interpretation_notes=({"requested": requested, "interpreted_as": month, "basis": basis},),
        data_basis=_data_basis(first, latest),
    )


def _unsupported(value: str, reason: str, first: str, latest: str) -> RelativePeriodResolution:
    return RelativePeriodResolution(
        unsupported=(UnsupportedFilter("relative_period" if "전" in value or value in _DAILY_TOKENS or value == "이번달" else "relative_range", value, reason),),
        data_basis=_data_basis(first, latest),
    )


def _available_months(periods: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted({period for period in periods if _MONTH_RE.fullmatch(period)}))


def _relative_month_delta(value: str) -> int | None:
    match = re.fullmatch(r"(\d{1,2})달전", value)
    return int(match.group(1)) if match else None


def _relative_year_delta(value: str) -> int | None:
    match = re.fullmatch(r"(\d{1,2})년전", value)
    return int(match.group(1)) if match else None


def _recent_month_count(value: str) -> int | None:
    match = re.fullmatch(r"최근\s*(\d{1,2})개월", value)
    return int(match.group(1)) if match else None


def _current_basis(current: str) -> str:
    return f"현재 {current} 기준 계산"


def _range_basis(current: str, requested_start: str, requested_end: str, latest: str, start: str, end: str) -> str:
    if start == requested_start and end == requested_end:
        return _current_basis(current)
    if requested_end > latest:
        return f"현재 {current} 기준 요청구간 {requested_start}~{requested_end} 중 최신 {latest}까지 제공"
    return f"현재 {current} 기준 요청구간 {requested_start}~{requested_end} 중 사용 가능 구간 {start}~{end} 제공"


def _shift_month(month: str, delta: int) -> str:
    year = int(month[:4])
    month_num = int(month[5:7])
    index = year * 12 + month_num - 1 + delta
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def _month_span(start: str, end: str) -> tuple[str, ...]:
    months: list[str] = []
    current = start
    while current <= end:
        months.append(current)
        current = _shift_month(current, 1)
    return tuple(months)


def _data_basis(first: str, latest: str) -> dict[str, str]:
    return {"period_grain": "monthly", "first_period": first, "latest_period": latest}
