from __future__ import annotations

from datetime import date
from difflib import get_close_matches
import re

from jw_chat_agent_poc.agentic import FilterEntry
from jw_chat_agent_poc.agent_loop.news_query import normalize_news_query


def period_filters(period: str | None) -> tuple[FilterEntry, ...]:
    if period in {None, "", "latest"}:
        return ()
    if period == "previous_year":
        return (("period", "previous_year"),)
    if re.fullmatch(r"20\d{2}-\d{2}", period):
        return (("period_month", period),)
    if period.isdigit():
        return (("period_year", int(period)),)
    return (("relative_period", period),)


def ground_news_query(raw_query: str, brand: str) -> str:
    query = normalize_news_query(raw_query, brand=brand)
    if raw_query.strip() and not query:
        raise ValueError("news query contains no searchable keyword after normalization")
    return query


def metric_measure(value: str) -> str:
    if value in {"share", "market_share", "ms", "rank"}:
        return "market_share"
    if value == "hhi":
        return "hhi"
    if value in {"series", "trend", "trajectory", "추이"}:
        return "series"
    return "sales"


def market_members(cache_brands: list[dict[str, object]], market_id: str) -> tuple[str, ...]:
    return tuple(str(item.get("brand")) for item in cache_brands if item.get("market_id") == market_id and item.get("brand"))


def system_current_month() -> str:
    today = date.today()
    return f"{today.year:04d}-{today.month:02d}"


def closest_allowed_brand(raw: str, allowed_brands: tuple[str, ...]) -> str | None:
    if not allowed_brands:
        return None
    matches = get_close_matches(raw, allowed_brands, n=1, cutoff=0.6)
    return matches[0] if matches else None
