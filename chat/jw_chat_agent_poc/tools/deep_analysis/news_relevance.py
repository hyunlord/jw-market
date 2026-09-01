from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EventMembership:
    event_key: str
    brands: frozenset[str]


def event_key(url: str, title: str, date: str, source: str) -> str:
    if url:
        return f"url:{url}"
    return "\u241f".join(("event", date, source, title))


def membership_matches(membership: EventMembership, requested: tuple[str, ...], operator: str) -> bool:
    if not requested:
        return True
    requested_set = frozenset(requested)
    if operator == "AND":
        return requested_set.issubset(membership.brands)
    return bool(requested_set & membership.brands)
