from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from enum import StrEnum
import re
from typing import Final

from jw_chat_agent_poc.orchestrator.query_spec import (
    RequestQuerySpec,
    TimeGranularity,
)


class PeriodGrain(StrEnum):
    MONTH = "month"
    QUARTER = "quarter"
    YEAR = "year"


class PeriodRequestKind(StrEnum):
    EXPLICIT_SET = "explicit_set"
    CLOSED_RANGE = "closed_range"
    TRAILING_WINDOW = "trailing_window"
    LATEST = "latest"


class PeriodResolution(StrEnum):
    RESOLVED = "resolved"
    UNVERIFIABLE = "unverifiable"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True, order=True)
class PeriodKey:
    grain: PeriodGrain
    value: str


@dataclass(frozen=True, slots=True)
class PeriodSelection:
    kind: PeriodRequestKind
    grain: PeriodGrain
    members: tuple[PeriodKey, ...]
    expected_count: int
    anchor: PeriodKey | None
    resolution: PeriodResolution


_MAX_PERIOD_MEMBERS: Final[int] = 60
_MONTH_RE: Final[re.Pattern[str]] = re.compile(r"^(20\d{2})-(0[1-9]|1[0-2])$")
_QUARTER_RE: Final[re.Pattern[str]] = re.compile(r"^(20\d{2})-Q([1-4])$")
_YEAR_RE: Final[re.Pattern[str]] = re.compile(r"^(20\d{2})$")


def period_selection_for_spec(
    spec: RequestQuerySpec,
    observed_periods: Collection[str],
) -> PeriodSelection | None:
    if (
        spec.start_period is not None
        and spec.end_period is not None
        and spec.start_period != spec.end_period
    ):
        return _closed_range(spec.start_period, spec.end_period)
    if (
        spec.window_count is not None
        and spec.granularity is TimeGranularity.QUARTER
    ):
        return _trailing_quarters(spec.window_count, observed_periods)
    return None


def canonical_observed_periods(
    periods: Collection[str],
    grain: PeriodGrain,
) -> tuple[PeriodKey, ...]:
    return tuple(
        sorted(
            {
                key
                for period in periods
                if (key := parse_period_key(period)) is not None
                and key.grain is grain
            }
        )
    )


def parse_period_key(value: str) -> PeriodKey | None:
    normalized = value.strip().upper()
    if _MONTH_RE.fullmatch(normalized):
        return PeriodKey(PeriodGrain.MONTH, normalized)
    if _QUARTER_RE.fullmatch(normalized):
        return PeriodKey(PeriodGrain.QUARTER, normalized)
    if _YEAR_RE.fullmatch(normalized):
        return PeriodKey(PeriodGrain.YEAR, normalized)
    return None


def _closed_range(start_value: str, end_value: str) -> PeriodSelection:
    start = parse_period_key(start_value)
    end = parse_period_key(end_value)
    if start is None or end is None or start.grain is not end.grain:
        return _unresolved_range(start, end, PeriodResolution.INVALID)
    start_index = _period_index(start)
    end_index = _period_index(end)
    if start_index > end_index:
        return _unresolved_range(start, end, PeriodResolution.INVALID)
    expected_count = end_index - start_index + 1
    if expected_count > _MAX_PERIOD_MEMBERS:
        return PeriodSelection(
            kind=PeriodRequestKind.CLOSED_RANGE,
            grain=start.grain,
            members=(),
            expected_count=expected_count,
            anchor=None,
            resolution=PeriodResolution.UNVERIFIABLE,
        )
    return PeriodSelection(
        kind=PeriodRequestKind.CLOSED_RANGE,
        grain=start.grain,
        members=tuple(
            _period_from_index(start.grain, index)
            for index in range(start_index, end_index + 1)
        ),
        expected_count=expected_count,
        anchor=None,
        resolution=PeriodResolution.RESOLVED,
    )


def _trailing_quarters(
    count: int,
    observed_periods: Collection[str],
) -> PeriodSelection:
    anchors = canonical_observed_periods(observed_periods, PeriodGrain.QUARTER)
    if count < 1:
        return _unresolved_window(count, None, PeriodResolution.INVALID)
    if count > _MAX_PERIOD_MEMBERS:
        return _unresolved_window(count, None, PeriodResolution.UNVERIFIABLE)
    if not anchors:
        return _unresolved_window(count, None, PeriodResolution.UNVERIFIABLE)
    anchor = anchors[-1]
    anchor_index = _period_index(anchor)
    return PeriodSelection(
        kind=PeriodRequestKind.TRAILING_WINDOW,
        grain=PeriodGrain.QUARTER,
        members=tuple(
            _period_from_index(PeriodGrain.QUARTER, index)
            for index in range(anchor_index - count + 1, anchor_index + 1)
        ),
        expected_count=count,
        anchor=anchor,
        resolution=PeriodResolution.RESOLVED,
    )


def _unresolved_range(
    start: PeriodKey | None,
    end: PeriodKey | None,
    resolution: PeriodResolution,
) -> PeriodSelection:
    grain = start.grain if start is not None else (
        end.grain if end is not None else PeriodGrain.MONTH
    )
    return PeriodSelection(
        kind=PeriodRequestKind.CLOSED_RANGE,
        grain=grain,
        members=(),
        expected_count=0,
        anchor=None,
        resolution=resolution,
    )


def _unresolved_window(
    count: int,
    anchor: PeriodKey | None,
    resolution: PeriodResolution,
) -> PeriodSelection:
    return PeriodSelection(
        kind=PeriodRequestKind.TRAILING_WINDOW,
        grain=PeriodGrain.QUARTER,
        members=(),
        expected_count=max(count, 0),
        anchor=anchor,
        resolution=resolution,
    )


def _period_index(period: PeriodKey) -> int:
    match period.grain:
        case PeriodGrain.MONTH:
            year, month = period.value.split("-")
            return int(year) * 12 + int(month) - 1
        case PeriodGrain.QUARTER:
            year, quarter = period.value.split("-Q")
            return int(year) * 4 + int(quarter) - 1
        case PeriodGrain.YEAR:
            return int(period.value)


def _period_from_index(grain: PeriodGrain, index: int) -> PeriodKey:
    match grain:
        case PeriodGrain.MONTH:
            year, month_index = divmod(index, 12)
            return PeriodKey(grain, f"{year:04d}-{month_index + 1:02d}")
        case PeriodGrain.QUARTER:
            year, quarter_index = divmod(index, 4)
            return PeriodKey(grain, f"{year:04d}-Q{quarter_index + 1}")
        case PeriodGrain.YEAR:
            return PeriodKey(grain, f"{index:04d}")
