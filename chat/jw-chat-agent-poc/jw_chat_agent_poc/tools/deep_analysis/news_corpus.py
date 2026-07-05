from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any, Final

from jw_chat_agent_poc.tools.deep_analysis.news_payload import DeepAnalysisNewsEvent


CORPUS_EVENT_LIMIT: Final[int] = 250


def corpus_events_sql() -> str:
    """Return the read-only query for brand-tagged full news corpus events."""

    return (
        "SELECT "
        "e.event_id, e.date, e.title, e.summary, e.body_full, "
        "e.source_name, e.source_url, e.category_label, e.category, "
        "MAX(s.score) AS impact_score "
        "FROM event_brand_scores s "
        "JOIN events e ON e.event_id = s.event_id "
        "WHERE s.brand_canonical = %s OR s.brand_name = %s "
        "GROUP BY e.event_id, e.date, e.title, e.summary, e.body_full, "
        "e.source_name, e.source_url, e.category_label, e.category "
        "ORDER BY e.date DESC, impact_score DESC "
        "LIMIT %s"
    )


def events_from_corpus_rows(rows: Sequence[Mapping[str, Any]]) -> tuple[DeepAnalysisNewsEvent, ...]:
    """Convert operational events/event_brand_scores rows into news events."""

    events: list[DeepAnalysisNewsEvent] = []
    for row in rows:
        event = _event_from_corpus_row(row)
        if event.title:
            events.append(event)
    return tuple(events)


def _event_from_corpus_row(row: Mapping[str, Any]) -> DeepAnalysisNewsEvent:
    return DeepAnalysisNewsEvent(
        date=_text(row.get("date")),
        title=_text(row.get("title")),
        source=_text(row.get("source_name") or row.get("source")),
        url=_safe_url(_text(row.get("source_url") or row.get("url"))),
        impact_score=_number(row.get("impact_score")),
        on_list=False,
        summary=_text(row.get("summary")),
        category=_text(row.get("category_label") or row.get("category")),
        body_full=_text(row.get("body_full") or row.get("article_text")),
    )


def _text(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value.strip() if isinstance(value, str) else ""


def _safe_url(value: str) -> str:
    if value.startswith(("https://", "http://")):
        return value
    return ""


def _number(value: Any) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    return None
