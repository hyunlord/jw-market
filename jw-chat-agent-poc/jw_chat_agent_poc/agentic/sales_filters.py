from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Final

from jw_chat_agent_poc.agentic.news_filters import FilterEntry, FilterValue, UnsupportedFilter
from jw_chat_agent_poc.agentic.sales_filter_extraction import extract_metric_filter_entries, metric_filter_entries_from_mapping
from jw_chat_agent_poc.agentic.sales_filter_aliases import (
    normalise_level,
    normalise_measure,
    normalise_source,
    normalise_channel,
)


_ALLOWED_KEYS: Final[frozenset[str]] = frozenset(
    {"source", "measure", "period", "period_year", "period_month", "relative_period", "relative_range", "channel", "level", "granularity"}
)
_UNSUPPORTED_REASONS: Final[dict[str, str]] = {
    "market_scope": "같은 시장/시장 전체 scope 필터는 아직 지원하지 않음",
}
@dataclass(frozen=True, slots=True)
class MetricFilterPlan:
    source: str | None = None
    measure: str | None = None
    period: str | None = None
    period_year: int | None = None
    period_month: str | None = None
    relative_period: str | None = None
    relative_range: str | None = None
    channel: str | None = None
    level: str | None = None
    unsupported: tuple[UnsupportedFilter, ...] = ()

    @property
    def has_effective_filter(self) -> bool:
        return any(
            value is not None
            for value in (
                self.source,
                self.period,
                self.period_year,
                self.period_month,
                self.relative_period,
                self.relative_range,
                self.channel,
                self.level,
            )
        ) or self.measure == "volume" or bool(self.unsupported)

    @property
    def blocks_results(self) -> bool:
        return bool(self.unsupported)

    def applied_filters(self, resolved_year: int | None = None) -> dict[str, FilterValue]:
        filters: dict[str, FilterValue] = {}
        if self.channel is not None:
            filters["channel"] = self.channel
        if self.level is not None:
            filters["level"] = self.level
        if self.measure is not None:
            filters["measure"] = self.measure
        if self.period is not None:
            filters["period"] = self.period
        if self.period_month is not None:
            filters["period_month"] = self.period_month
        if self.period_year is not None:
            filters["period_year"] = self.period_year
        if resolved_year is not None:
            filters["period_year"] = resolved_year
        if self.source is not None:
            filters["source"] = self.source
        return filters


def validate_metric_filters(entries: tuple[FilterEntry, ...]) -> MetricFilterPlan:
    unsupported: list[UnsupportedFilter] = []
    source: str | None = None
    measure: str | None = None
    period: str | None = None
    period_year: int | None = None
    period_month: str | None = None
    relative_period: str | None = None
    relative_range: str | None = None
    channel: str | None = None
    level: str | None = None

    for field, value in entries:
        if field not in _ALLOWED_KEYS:
            unsupported.append(UnsupportedFilter(field, str(value), _UNSUPPORTED_REASONS.get(field, "지원하지 않는 매출 필터")))
            continue
        if field == "source":
            source = normalise_source(str(value))
            if source is None:
                unsupported.append(UnsupportedFilter("source", str(value), "지원하지 않는 매출 source"))
        elif field == "measure":
            measure = normalise_measure(str(value))
            if measure is None:
                unsupported.append(UnsupportedFilter("measure", str(value), "sales/volume만 지원"))
        elif field == "period":
            period = _normalise_period(str(value), unsupported)
        elif field == "period_year":
            period_year = _year_value(value, unsupported)
        elif field == "period_month":
            period_month = _period_month(str(value), unsupported)
        elif field == "relative_period":
            relative_period = str(value)
        elif field == "relative_range":
            relative_range = str(value)
        elif field == "channel":
            channel = normalise_channel(str(value))
        elif field == "level":
            level = normalise_level(str(value))
        elif field == "granularity":
            unsupported.append(UnsupportedFilter("granularity", str(value), "병원별/지역별 매출은 cache에 없음"))

    return MetricFilterPlan(
        source=source,
        measure=measure,
        period=period,
        period_year=period_year,
        period_month=period_month,
        relative_period=relative_period,
        relative_range=relative_range,
        channel=channel,
        level=level,
        unsupported=tuple(unsupported),
    )

def _normalise_period(value: str, unsupported: list[UnsupportedFilter]) -> str | None:
    if value == "previous_year":
        return value
    unsupported.append(UnsupportedFilter("period", value, "previous_year 또는 연/월만 지원"))
    return None


def _year_value(value: FilterValue, unsupported: list[UnsupportedFilter]) -> int | None:
    year = int(value) if isinstance(value, int | float) or str(value).isdigit() else 0
    if 2000 <= year <= 2100:
        return year
    unsupported.append(UnsupportedFilter("period_year", str(value), "YYYY 연도만 지원"))
    return None


def _period_month(value: str, unsupported: list[UnsupportedFilter]) -> str | None:
    if re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", value):
        return value
    unsupported.append(UnsupportedFilter("period_month", value, "YYYY-MM만 지원"))
    return None
