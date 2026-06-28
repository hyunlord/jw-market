from __future__ import annotations

import re
from typing import Any

from .config import ValidatorConfig


DATE_PATTERNS = (
    re.compile(r"(\d{4}-\d{2}-\d{2})"),
    re.compile(r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일"),
)


def _event_dates(bundle: dict[str, Any]) -> set[str]:
    dates: set[str] = set()
    event_bundle = bundle.get("event_bundle", {}) or {}
    for list_name in ("events_brand_centric", "events_market_trend", "cross_match_events"):
        for event in event_bundle.get(list_name, []) or []:
            date = event.get("published_date")
            if date:
                dates.add(str(date)[:10])

    by_source = ((bundle.get("competitor_events", {}) or {}).get("by_source", {}) or {})
    for source_payload in by_source.values():
        for comp in source_payload.get("competitors", []) or []:
            for event in comp.get("events", []) or []:
                date = event.get("published_date")
                if date:
                    dates.add(str(date)[:10])
    by_view = ((bundle.get("competitor_events", {}) or {}).get("by_view", {}) or {})
    for view_payload in by_view.values():
        for comp in view_payload.get("competitors", []) or []:
            for event in comp.get("events", []) or []:
                date = event.get("published_date")
                if date:
                    dates.add(str(date)[:10])
    return dates


def _narrative_text(parsed_output: dict[str, Any]) -> str:
    chunks: list[str] = []
    for stage in ("phenomenon", "cause", "prediction", "recommendation"):
        stage_obj = parsed_output.get(stage, {}) or {}
        chunks.append(str(stage_obj.get("title", "")))
        chunks.append(str(stage_obj.get("body", "")))
        chunks.extend(str(bullet) for bullet in stage_obj.get("bullets", []) or [])
    return "\n".join(chunks)


def _cited_dates(text: str) -> set[str]:
    dates: set[str] = set()
    for match in DATE_PATTERNS[0].finditer(text):
        dates.add(match.group(1))
    for match in DATE_PATTERNS[1].finditer(text):
        year, month, day = match.groups()
        dates.add(f"{year}-{int(month):02d}-{int(day):02d}")
    return dates


def validate_narrative_events(parsed_output: dict[str, Any], bundle: dict[str, Any], _config: ValidatorConfig) -> dict[str, Any]:
    event_dates = _event_dates(bundle)
    cited_dates = _cited_dates(_narrative_text(parsed_output))
    unmatched = []
    for date in sorted(cited_dates - event_dates):
        unmatched.append(
            {
                "date": date,
                "found_in_events": False,
                "closest_event_dates": sorted(d for d in event_dates if d[:7] == date[:7])[:3],
            }
        )

    return {
        "valid": not unmatched,
        "total_cited_dates": len(cited_dates),
        "total_event_dates_in_bundle": len(event_dates),
        "matched_dates": len(cited_dates) - len(unmatched),
        "unmatched_dates": unmatched,
        "cited_dates_set": sorted(cited_dates),
        "note": "Layer 4 is best-effort; config may treat unmatched event dates as warnings.",
    }
