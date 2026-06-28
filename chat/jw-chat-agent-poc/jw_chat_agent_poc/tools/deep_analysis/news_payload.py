from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class DeepAnalysisNewsEvent:
    date: str
    title: str
    source: str
    url: str
    impact_score: float | None
    on_list: bool
    summary: str
    category: str
    body_full: str

    def to_render_item(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "title": self.title,
            "source": self.source,
            "url": self.url,
            "impact_score": self.impact_score,
            "on_list": self.on_list,
            "summary": self.summary,
            "category": self.category,
        }


def events_from_payload(payload: dict[str, Any]) -> list[DeepAnalysisNewsEvent]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    raw_events = data.get("events") if isinstance(data, dict) else None
    if not isinstance(raw_events, list):
        return []
    events: list[DeepAnalysisNewsEvent] = []
    for raw in raw_events:
        if not isinstance(raw, dict):
            continue
        event = _event_from_raw(raw)
        if event.title:
            events.append(event)
    return events


def _event_from_raw(raw: dict[str, Any]) -> DeepAnalysisNewsEvent:
    body = _text(raw.get("body_full") or raw.get("body") or raw.get("content"))
    title = _text(raw.get("title") or raw.get("news_title") or raw.get("headline")) or _title_from_body(body)
    return DeepAnalysisNewsEvent(
        date=_text(raw.get("date") or raw.get("event_date") or raw.get("published_date") or raw.get("published_at")),
        title=title,
        source=_text(raw.get("source") or raw.get("source_name") or raw.get("publisher")),
        url=_safe_url(_text(raw.get("url") or raw.get("news_url") or raw.get("link"))),
        impact_score=_number(raw.get("impact_score") or raw.get("score")),
        on_list=_bool(raw.get("on_list")),
        summary=_text(raw.get("summary") or raw.get("summary_text") or raw.get("body_summary") or body),
        category=_text(raw.get("category") or raw.get("event_category") or raw.get("topic")),
        body_full=body,
    )


def _title_from_body(body: str) -> str:
    if not body:
        return ""
    return body[:80].rstrip()


def _safe_url(value: str) -> str:
    if value.startswith(("https://", "http://")):
        return value
    return ""


def _text(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value.strip() if isinstance(value, str) else ""


def _number(value: Any) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y"}
    return False
