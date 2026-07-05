from __future__ import annotations

from typing import Any


def view_label(view_type: str) -> str:
    return "경쟁군" if view_type == "competitive_dynamics" else "전략뷰"


def market_view_notices(view_type: str) -> list[str]:
    return []


def extended(card: dict[str, Any]) -> dict[str, Any]:
    value = card.get("back_extended", {})
    return value if isinstance(value, dict) else {}


def find_brand_card(market_status: dict[str, Any], brand: str) -> dict[str, Any]:
    cards = market_status.get("brand_cards", [])
    if not isinstance(cards, list):
        raise TypeError("cache_market_status.brand_cards must be a list")
    for card in cards:
        if isinstance(card, dict) and card.get("brand") == brand:
            return card
    raise LookupError(f"Unknown brand card: {brand}")


def find_brand_bridge(cache_brands: list[dict[str, Any]], brand: str) -> dict[str, Any]:
    for item in cache_brands:
        if item.get("brand") == brand:
            return item
    return {}


def source_label(card: dict[str, Any], bridge: dict[str, Any]) -> str:
    front = card.get("front", {})
    card_extended = extended(card)
    sources = bridge.get("sources")
    if isinstance(front, dict) and isinstance(front.get("default_source"), str):
        return front["default_source"]
    if isinstance(card_extended.get("source_label"), str):
        return card_extended["source_label"]
    if isinstance(sources, list) and sources:
        return str(sources[0])
    return "UBIST"


def period_recent(market_status: dict[str, Any], card: dict[str, Any]) -> str:
    front = card.get("front", {})
    source = front.get("default_source") if isinstance(front, dict) else None
    summary = market_status.get("kpi_summary", {})
    if isinstance(source, str) and isinstance(summary, dict):
        source_summary = summary.get(source, {})
        if isinstance(source_summary, dict) and isinstance(source_summary.get("period_recent"), str):
            return source_summary["period_recent"]
    return ""
