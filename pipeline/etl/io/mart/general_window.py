from __future__ import annotations

from collections.abc import Iterable

import pandas as pd

from .general_config import IQVIA_HISTORY_PERIODS, UBIST_HISTORY_PERIODS
from .layer3_normalize import parse_period, period_sort_key


_WINDOW_PERIODS_BY_SOURCE = {
    "ubist": UBIST_HISTORY_PERIODS,
    "iqvia_nsa": IQVIA_HISTORY_PERIODS,
}


def canonical_period_label(value: object) -> str:
    text = str(value or "").strip()
    if len(text) == 6 and text.isdigit():
        text = f"{text[:4]}-{text[4:]}"
    elif len(text) == 6 and text[4].upper() == "Q":
        text = f"{text[:4]}-Q{text[5]}"
    info = parse_period(text)
    if info.kind == "month":
        return f"{info.year:04d}-{info.month:02d}"
    return f"{info.year:04d}-Q{info.quarter}"


def rolling_period_scope(
    periods: Iterable[object],
    *,
    source: str,
) -> tuple[str, ...]:
    try:
        window_periods = _WINDOW_PERIODS_BY_SOURCE[source]
    except KeyError as exc:
        raise ValueError(f"unsupported rolling-window source: {source!r}") from exc
    canonical = {
        canonical_period_label(period)
        for period in periods
        if str(period or "").strip()
    }
    ordered = tuple(sorted(canonical, key=period_sort_key))
    return ordered[-window_periods:]


def filter_frame_to_rolling_window(
    frame: pd.DataFrame,
    *,
    source: str,
    period_column: str = "period_yyyymm",
) -> pd.DataFrame:
    if frame.empty or period_column not in frame.columns:
        return frame
    scope = set(rolling_period_scope(frame[period_column].tolist(), source=source))
    canonical = frame[period_column].map(canonical_period_label)
    return frame.loc[canonical.isin(scope)].copy()
