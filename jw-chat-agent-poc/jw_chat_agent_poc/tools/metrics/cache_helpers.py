from __future__ import annotations

from typing import Any


class CacheMetricHelperMixin:
    @staticmethod
    def _find_brand_bridge(cache_brands: list[dict[str, Any]], brand: str) -> dict[str, Any]:
        for item in cache_brands:
            if item.get("brand") == brand:
                return item
        raise LookupError(f"Unknown cache brand: {brand}")

    @staticmethod
    def _find_brand_card(market_status: dict[str, Any], brand: str) -> dict[str, Any]:
        cards = market_status.get("brand_cards", [])
        if not isinstance(cards, list):
            raise TypeError("cache_market_status.brand_cards must be a list")
        for card in cards:
            if isinstance(card, dict) and card.get("brand") == brand:
                return card
        raise LookupError(f"Unknown brand card: {brand}")

    @staticmethod
    def _period_recent(market_status: dict[str, Any], card: dict[str, Any]) -> str:
        front = card.get("front", {})
        default_source = front.get("default_source")
        summary = market_status.get("kpi_summary", {})
        if isinstance(default_source, str) and isinstance(summary, dict):
            source_summary = summary.get(default_source, {})
            if isinstance(source_summary, dict) and isinstance(source_summary.get("period_recent"), str):
                return source_summary["period_recent"]
        return ""

    @staticmethod
    def _unsupported(brand: str | None, metric: str, message: str) -> dict[str, Any]:
        return {
            "source": "cache",
            "tool": "unsupported_metric",
            "summary_text": message,
            "render_data": {"brand": brand, "metric": metric, "status": "unsupported", "message": message},
        }

    @staticmethod
    def _first_source(bridge: dict[str, Any]) -> str | None:
        sources = bridge.get("sources")
        if isinstance(sources, list) and sources:
            return str(sources[0])
        return None

    @staticmethod
    def _krw_to_eok(value: Any) -> float | None:
        if isinstance(value, int | float):
            return round(float(value) / 100_000_000, 2)
        return None

    @classmethod
    def _format_krw(cls, value: Any) -> str:
        eok = cls._krw_to_eok(value)
        if eok is None:
            return "N/A"
        return f"{eok:,.2f}억원"

    @staticmethod
    def _format_pct(value: Any) -> str:
        if isinstance(value, int | float):
            return f"{float(value):.2f}%"
        return "N/A"
