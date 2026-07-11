from __future__ import annotations

import re
from typing import Mapping

from jw_chat_agent_poc.agentic.news_filters import FilterEntry, FilterValue
from jw_chat_agent_poc.agentic.sales_filter_aliases import (
    LEVEL_ALIASES,
    SOURCE_ALIASES,
    match_channel_in_text,
)


def extract_metric_filter_entries(question: str) -> tuple[FilterEntry, ...]:
    entries: list[FilterEntry] = []
    source = _source_from_question(question)
    if source is not None:
        entries.append(("source", source))
    measure = _measure_from_question(question)
    if measure is not None:
        entries.append(("measure", measure))
    entries.extend(_period_entries(question))
    entries.extend(_unsupported_temporal_entries(question))
    market_scope = _unsupported_market_scope(question)
    if market_scope is not None:
        entries.append(("market_scope", market_scope))
    unsupported = _unsupported_granularity(question)
    if unsupported is not None:
        entries.append(("granularity", unsupported))
    else:
        channel = _channel_from_question(question)
        if channel is not None:
            entries.append(("channel", channel))
    level = _level_from_question(question)
    if level is not None:
        entries.append(("level", level))
    return _dedupe_entries(tuple(entries))


def metric_filter_entries_from_mapping(raw: Mapping[str, FilterValue] | None) -> tuple[FilterEntry, ...]:
    if raw is None:
        return ()
    entries = tuple((str(key), value) for key, value in raw.items() if isinstance(value, str | int | float | bool))
    return _dedupe_entries(entries)


def _source_from_question(question: str) -> str | None:
    upper = question.upper()
    for alias, value in SOURCE_ALIASES.items():
        if alias in question or alias in upper:
            return value
    return None


def _measure_from_question(question: str) -> str | None:
    lower = question.lower()
    if any(token in question for token in ("처방량", "수량")) or "volume" in lower:
        return "volume"
    if any(token in question for token in ("매출", "판매", "처방조제액")) or "sales" in lower:
        return "sales"
    return None


def _period_entries(question: str) -> list[FilterEntry]:
    entries: list[FilterEntry] = []
    if any(token in question for token in ("작년", "지난해", "전년")):
        entries.append(("period", "previous_year"))
    month = re.search(r"(20\d{2})[-.년\s]+(0?[1-9]|1[0-2])\s*(?:월)?", question)
    if month:
        entries.append(("period_month", f"{month.group(1)}-{int(month.group(2)):02d}"))
        return entries
    year = re.search(r"(20\d{2})\s*년?", question)
    if year:
        entries.append(("period_year", int(year.group(1))))
    return entries


def _unsupported_temporal_entries(question: str) -> tuple[FilterEntry, ...]:
    entries: list[FilterEntry] = []
    for token in ("오늘", "어제", "하루전", "이번달"):
        if token in question:
            entries.append(("relative_period", token))
            break
    previous_day = re.search(r"(\d{1,2})\s*일\s*전", question)
    if previous_day:
        entries.append(("relative_period", f"{int(previous_day.group(1))}일전"))
    previous_month = re.search(r"(\d{1,2})\s*(?:달|개월)\s*전", question)
    if previous_month:
        entries.append(("relative_period", f"{int(previous_month.group(1))}달전"))
    previous_year = re.search(r"(\d{1,2})\s*년\s*전", question)
    if previous_year:
        entries.append(("relative_period", f"{int(previous_year.group(1))}년전"))
    if any(token in question for token in ("최근 한 달", "최근 한달", "최근 1달", "최근 1개월")):
        entries.append(("relative_range", "최근 1개월"))
    recent_months = re.search(r"최근\s*(\d{1,2})\s*(?:달|개월)", question)
    if recent_months:
        entries.append(("relative_range", f"최근 {int(recent_months.group(1))}개월"))
    return tuple(entries)


def _unsupported_market_scope(question: str) -> str | None:
    for token in ("같은 시장", "시장 전체", "해당 시장"):
        if token in question:
            return token
    return None


def _unsupported_granularity(question: str) -> str | None:
    for token, label in (("병원별", "individual_hospital"), ("기관별", "individual_institution"), ("지역별", "region")):
        if token in question:
            return label
    return None


def _channel_from_question(question: str) -> str | None:
    return match_channel_in_text(question)


def _level_from_question(question: str) -> str | None:
    lower = question.lower()
    for alias, value in LEVEL_ALIASES.items():
        if alias in question or alias in lower:
            return value
    return None


def _dedupe_entries(entries: tuple[FilterEntry, ...]) -> tuple[FilterEntry, ...]:
    seen: set[str] = set()
    out: list[FilterEntry] = []
    for field, value in sorted(entries, key=lambda item: item[0]):
        if field in seen:
            continue
        seen.add(field)
        out.append((field, value))
    return tuple(out)
