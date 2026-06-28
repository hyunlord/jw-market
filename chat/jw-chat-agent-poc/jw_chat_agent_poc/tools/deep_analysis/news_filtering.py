from __future__ import annotations

from datetime import date
from typing import Protocol, TypeVar

from jw_chat_agent_poc.agentic import NewsFilterPlan, normalise_news_source
from jw_chat_agent_poc.agentic.news_text import normalized_contains


class NewsEventForFilter(Protocol):
    date: str
    title: str
    source: str
    impact_score: float | None
    on_list: bool
    summary: str
    category: str
    body_full: str


TNewsEvent = TypeVar("TNewsEvent", bound=NewsEventForFilter)


def filter_events(
    events: tuple[TNewsEvent, ...],
    plan: NewsFilterPlan,
    latest_event_date: str,
) -> tuple[TNewsEvent, ...]:
    return tuple(event for event in events if _matches_filter(event, plan, latest_event_date))


def select_events(
    events: tuple[TNewsEvent, ...],
    limit: int,
    prioritize_impact: bool = False,
) -> tuple[TNewsEvent, ...]:
    on_list = tuple(event for event in events if event.on_list)
    candidates = on_list or events
    key = _impact_sort_key if prioritize_impact else _sort_key
    return tuple(sorted(candidates, key=key, reverse=True)[:limit])


def _matches_filter(event: NewsEventForFilter, plan: NewsFilterPlan, latest_event_date: str) -> bool:
    event_source = normalise_news_source(event.source) or event.source
    if plan.source is not None and event_source != plan.source:
        return False
    if plan.category is not None and plan.category not in event.category and plan.category not in event.summary and plan.category not in event.title:
        return False
    if plan.min_impact_score is not None and (event.impact_score is None or event.impact_score < plan.min_impact_score):
        return False
    if plan.title_text is not None and not normalized_contains(event.title, plan.title_text):
        return False
    if plan.content_text is not None and not normalized_contains(_content_text(event), plan.content_text):
        return False
    if plan.any_text is not None and not normalized_contains(f"{event.title}\n{_content_text(event)}", plan.any_text):
        return False
    return _matches_date_filter(event, plan, latest_event_date)


def _content_text(event: NewsEventForFilter) -> str:
    return f"{event.body_full}\n{event.summary}" if event.body_full else event.summary


def _matches_date_filter(event: NewsEventForFilter, plan: NewsFilterPlan, latest_event_date: str) -> bool:
    event_date = _parse_event_date(event.date)
    if event_date is None:
        return plan.date_from is None and plan.date_to is None and plan.recent_days is None
    start = _parse_event_date(plan.date_from or "")
    end = _parse_event_date(plan.date_to or "")
    if plan.recent_days is not None and start is None:
        latest = _parse_event_date(latest_event_date)
        if latest is not None:
            start = date.fromordinal(latest.toordinal() - plan.recent_days)
            end = latest
    if start is not None and event_date < start:
        return False
    if end is not None and event_date > end:
        return False
    return True


def _parse_event_date(value: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _sort_key(event: NewsEventForFilter) -> tuple[str, float]:
    return (event.date, event.impact_score if event.impact_score is not None else -1.0)


def _impact_sort_key(event: NewsEventForFilter) -> tuple[float, str]:
    return (event.impact_score if event.impact_score is not None else -1.0, event.date)
