from __future__ import annotations

from typing import Any, Mapping

from jw_chat_agent_poc.agentic import (
    FilterEntry,
    FilterValue,
    extract_metric_filter_entries,
    extract_news_filter_entries,
    filter_entries_from_mapping,
    metric_filter_entries_from_mapping,
)
from jw_chat_agent_poc.router.llm_router_prompts import has_metric_filter_cue, has_news_filter_cue


def filters_for_sources(data: Mapping[str, Any], question: str, sources: tuple[str, ...]) -> tuple[FilterEntry, ...]:
    if "deep_analysis_events" in sources:
        return _news_filters(data, question)
    if "metrics" in sources:
        return _metric_filters(data, question)
    return ()


def _news_filters(data: Mapping[str, Any], question: str) -> tuple[FilterEntry, ...]:
    question_filters = extract_news_filter_entries(question)
    if question_filters:
        return question_filters
    if not has_news_filter_cue(question):
        return ()
    raw_filters = data.get("filters")
    llm_filter_values: dict[str, FilterValue] = {}
    if isinstance(raw_filters, Mapping):
        for key, value in raw_filters.items():
            if isinstance(key, str) and isinstance(value, str | int | float | bool):
                llm_filter_values[key] = value
    return filter_entries_from_mapping(llm_filter_values)


def _metric_filters(data: Mapping[str, Any], question: str) -> tuple[FilterEntry, ...]:
    question_filters = extract_metric_filter_entries(question)
    if question_filters:
        return question_filters
    if not has_metric_filter_cue(question):
        return ()
    raw_filters = data.get("filters")
    llm_filter_values: dict[str, FilterValue] = {}
    if isinstance(raw_filters, Mapping):
        for key, value in raw_filters.items():
            if isinstance(key, str) and isinstance(value, str | int | float | bool):
                llm_filter_values[key] = value
    return metric_filter_entries_from_mapping(llm_filter_values)
