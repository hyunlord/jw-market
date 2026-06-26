from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from jw_chat_agent_poc.agentic import FilterEntry, validate_metric_filters
from jw_chat_agent_poc.tools.metrics.cache_cause_metrics import CauseMetricMixin
from jw_chat_agent_poc.tools.metrics.cache_helpers import CacheMetricHelperMixin
from jw_chat_agent_poc.tools.metrics.cache_live import (
    CausePayloadReader,
    MariaDbCausePayloadReader,
    MariaDbMetricsCacheReader,
    MetricsCacheReader,
    TtlCausePayloadCache,
    TtlMetricsCache,
)


class MetricsTool(CauseMetricMixin, CacheMetricHelperMixin):
    def __init__(
        self,
        fixture_path: Path | None = None,
        mode: str | None = None,
        cache_reader: MetricsCacheReader | None = None,
        cause_reader: CausePayloadReader | None = None,
        ttl_seconds: int | None = None,
    ) -> None:
        self._mode = mode or os.environ.get("CHAT_METRICS_MODE", "fixture")
        path = fixture_path or Path(__file__).resolve().parents[2] / "fixtures" / "metrics_cache.json"
        self._data = json.loads(path.read_text(encoding="utf-8"))
        ttl = ttl_seconds or int(os.environ.get("CHAT_METRICS_TTL_SECONDS", "300"))
        self._cache = TtlMetricsCache(cache_reader or MariaDbMetricsCacheReader(), ttl_seconds=ttl)
        cause_ttl = int(os.environ.get("CHAT_CAUSE_TTL_SECONDS", str(ttl)))
        self._cause_cache = TtlCausePayloadCache(cause_reader or MariaDbCausePayloadReader(), ttl_seconds=cause_ttl)

    def get_market_landscape(self, market: str, level: str = "overall", view_type: str = "market_landscape") -> dict:
        if self._mode == "cache":
            return self._get_market_card_metric(market, level, view_type)
        item = self._data["markets"].get(market)
        if not item:
            raise LookupError(f"Unknown market fixture: {market}")
        return {
            "source": "cache",
            "tool": "get_market_landscape",
            "summary_text": (
                f"{item['label']} 시장은 {item['latest_period']} 기준 {item['market_size_krw']}원, "
                f"5년 CAGR {item['cagr_5y_pct']}%, HHI {item['hhi']}입니다."
            ),
            "render_data": {
                "market": market,
                "level": level,
                "view_type": view_type,
                "series": item["market_size_series"],
                "cagr_5y_pct": item["cagr_5y_pct"],
                "hhi": item["hhi"],
            },
        }

    def get_brand_metric(
        self,
        brand: str,
        metric: str = "market_share",
        period: str = "2026-04",
        filter_entries: tuple[FilterEntry, ...] = (),
    ) -> dict:
        if self._mode == "cache":
            return self._get_brand_card_metric(brand, metric, period, filter_entries)
        item = self._data["brands"].get(brand)
        if not item:
            raise LookupError(f"Unknown brand fixture: {brand}")
        if metric in {"hhi", "series", "trend"}:
            market_id = "ml_006" if brand in {"리바로", "리바로젯"} else "mock_market"
            market = self._data["markets"].get(market_id, {})
            hhi = market.get("hhi")
            return {
                "source": "cache",
                "tool": "get_brand_metric",
                "summary_text": f"{brand} 시장의 fixture HHI는 {hhi}입니다.",
                "render_data": {
                    "brand": brand,
                    "metric": metric,
                    "period": market.get("latest_period"),
                    "hhi_recent": hhi,
                    "market_size_series": [
                        {"period": period, "value_krw": value, "value_억원": self._krw_to_eok(value)}
                        for period, value in sorted(market.get("market_size_series", {}).items())
                    ],
                },
            }
        value = item.get(metric)
        return {
            "source": "cache",
            "tool": "get_brand_metric",
            "summary_text": f"{brand}의 {period} {metric} 값은 {value}입니다.",
            "render_data": {"brand": brand, "metric": metric, "period": period, "value": value, **item},
        }

    def _get_brand_card_metric(
        self,
        brand: str,
        metric: str,
        period: str,
        filter_entries: tuple[FilterEntry, ...] = (),
    ) -> dict[str, Any]:
        if filter_entries:
            plan = validate_metric_filters(filter_entries)
            if plan.has_effective_filter:
                return self._get_filtered_cause_metric(brand, metric, filter_entries)
        if self._is_cause_metric(metric):
            return self._get_cause_metric(brand, metric)

        snapshot = self._cache.snapshot()
        bridge = self._find_brand_bridge(snapshot.cache_brands, brand)
        card = self._find_brand_card(snapshot.market_status, brand)
        front = card.get("front", {})
        back = card.get("back", {})
        extended = card.get("back_extended", {})
        period_recent = self._period_recent(snapshot.market_status, card)

        sales = front.get("value_recent")
        ms = front.get("ms_recent_pct")
        rank = card.get("rank")
        total = card.get("total_brands_in_market")
        market_size = extended.get("market_size_recent")
        brand_cagr = extended.get("brand_cagr_5y_pct", back.get("cagr_5y_pct"))
        market_cagr = extended.get("market_cagr_5y_pct")
        excess = extended.get("excess_growth_pct")
        source = front.get("default_source") or extended.get("source_label") or self._first_source(bridge)

        return {
            "source": "cache",
            "tool": "get_brand_metric",
            "summary_text": (
                f"{brand}의 {period_recent} 최신 매출은 {self._format_krw(sales)}, MS {self._format_pct(ms)}, "
                f"순위 {rank}/{total}, 시장규모 {self._format_krw(market_size)}입니다. "
                f"브랜드 CAGR {self._format_pct(brand_cagr)}, 시장 CAGR {self._format_pct(market_cagr)}, "
                f"excess growth {self._format_pct(excess)} 기준입니다."
            ),
            "render_data": {
                "brand": brand,
                "metric": metric,
                "period": period_recent or period,
                "market_id": card.get("market_id") or bridge.get("market_id"),
                "market_name": card.get("market_name") or bridge.get("market_name"),
                "source_label": source,
                "sales_krw": sales,
                "sales_억원": self._krw_to_eok(sales),
                "ms_recent_pct": ms,
                "rank": rank,
                "total_brands_in_market": total,
                "market_size_recent_krw": market_size,
                "market_size_억원": self._krw_to_eok(market_size),
                "brand_cagr_5y_pct": brand_cagr,
                "market_cagr_5y_pct": market_cagr,
                "excess_growth_pct": excess,
                "gr_mom_pct": front.get("gr_mom_pct"),
                "gr_qoq_pct": front.get("gr_qoq_pct"),
                "gr_yoy_pct": front.get("gr_yoy_pct"),
                "gr_yoy_mat_pct": front.get("gr_yoy_mat_pct"),
                "gr_yoy_ym_pct": front.get("gr_yoy_ym_pct"),
            },
        }

    def _get_market_card_metric(self, market: str, level: str, view_type: str) -> dict[str, Any]:
        snapshot = self._cache.snapshot()
        market_id = "strategy_006" if market == "ml_006" else market
        cards = snapshot.market_status.get("brand_cards", [])
        if not isinstance(cards, list):
            raise TypeError("cache_market_status.brand_cards must be a list")
        card = next((item for item in cards if isinstance(item, dict) and item.get("market_id") == market_id), None)
        if card is None:
            raise LookupError(f"Unknown cache market: {market}")

        front = card.get("front", {})
        extended = card.get("back_extended", {})
        period_recent = self._period_recent(snapshot.market_status, card) or "latest"
        return {
            "source": "cache",
            "tool": "get_market_landscape",
            "summary_text": (
                f"{card.get('market_name', market_id)} 시장은 {period_recent} 기준 "
                f"{self._format_krw(extended.get('market_size_recent'))}, "
                f"시장 CAGR {self._format_pct(extended.get('market_cagr_5y_pct'))}입니다."
            ),
            "render_data": {
                "market": market_id,
                "level": level,
                "view_type": view_type,
                "period": period_recent,
                "market_size_recent_krw": extended.get("market_size_recent"),
                "market_size_억원": self._krw_to_eok(extended.get("market_size_recent")),
                "market_cagr_5y_pct": extended.get("market_cagr_5y_pct"),
                "source_label": front.get("default_source") or extended.get("source_label"),
            },
        }
