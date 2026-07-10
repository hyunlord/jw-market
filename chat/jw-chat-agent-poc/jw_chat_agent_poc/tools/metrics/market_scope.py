from __future__ import annotations

import os
from typing import Any

from jw_chat_agent_poc.orchestrator.markdown_formatting import eok_value
from jw_chat_agent_poc.orchestrator.markdown_response import MarkdownResponseBuilder
from jw_chat_agent_poc.resolver import BrandResolver, UnsupportedBrandError
from jw_chat_agent_poc.service.general_view_routing import GeneralRoute, GeneralViewService
from jw_chat_agent_poc.tools.metrics.cache_live import (
    CausePayloadKey,
    CausePayloadReader,
    MariaDbMetricsCacheReader,
    MetricsCacheReader,
    TtlCausePayloadCache,
    TtlMetricsCache,
    shared_cause_payload_cache,
    shared_metrics_cache,
)
from jw_chat_agent_poc.tools.metrics.cd_mart import (
    CdMartReader,
    MariaDbCdMartReader,
    TtlCdMartCache,
)
from jw_chat_agent_poc.tools.metrics.market_scope_intent import (
    MarketView,
    detect_market_scope_intent,
    map_market_view_reply,
)
from jw_chat_agent_poc.tools.metrics.market_scope_helpers import (
    extended,
    find_brand_bridge,
    find_brand_card,
    market_view_notices,
    period_recent,
    source_label,
    view_label,
)


class MarketScopeResolver:
    def __init__(
        self,
        *,
        cache_reader: MetricsCacheReader | None = None,
        cause_reader: CausePayloadReader | None = None,
        cd_mart_reader: CdMartReader | None = None,
        ttl_seconds: int | None = None,
        general_view_service: GeneralViewService | None = None,
    ) -> None:
        ttl = ttl_seconds or int(os.environ.get("CHAT_MARKET_SCOPE_TTL_SECONDS", "300"))
        self._reader = cache_reader or MariaDbMetricsCacheReader()
        self._cache = TtlMetricsCache(cache_reader, ttl_seconds=ttl) if cache_reader is not None else shared_metrics_cache(ttl)
        self._cause_cache = (
            TtlCausePayloadCache(cause_reader, ttl_seconds=ttl)
            if cause_reader is not None
            else shared_cause_payload_cache(ttl)
        )
        self._cd_mart_cache = TtlCdMartCache(cd_mart_reader or MariaDbCdMartReader(), ttl_seconds=ttl)
        self._resolver = BrandResolver(mode="cache", brand_reader=cache_reader, ttl_seconds=ttl)
        self._general_view = general_view_service or GeneralViewService.from_env(self._resolver)

    def general_route(self, question: str) -> GeneralRoute:
        return self._general_view.route(question)

    def answer_general(self, question: str, *, compact: bool, dual: bool) -> dict[str, Any]:
        return self._general_view.answer(question, compact=compact, dual=dual)

    def answer(self, question: str, *, view_type: MarketView) -> dict[str, Any]:
        if view_type == "general_view":
            return self.unsupported_general_view(question)
        try:
            resolution = self._resolver.resolve(question, allow_default=False)
            snapshot = self._cache.snapshot()
            card = find_brand_card(snapshot.market_status, resolution.canonical_brand)
        except (LookupError, UnsupportedBrandError) as exc:
            return self._unsupported(str(exc), question, "brand", "unknown")

        market_id = str(card.get("market_id") or resolution.market_id or "")
        source = source_label(card, find_brand_bridge(snapshot.cache_brands, resolution.canonical_brand))
        period, market_size, yoy = self._view_market_size(
            brand=resolution.canonical_brand,
            view_type=view_type,
            source=source,
            market_id=market_id,
        )
        if market_size is None:
            if view_type == "market_landscape":
                period = period_recent(snapshot.market_status, card) or "latest"
                market_size = extended(card).get("market_size_recent")
            else:
                return self._unsupported(
                    "competitive_dynamics 시장규모를 전략 CD mart에서 찾지 못했습니다.",
                    question,
                    "view_type",
                    view_type,
                )
        data = self._render_data(card, market_id, source, period or "latest", market_size, yoy, view_type)
        market_view_label = view_label(view_type)
        call = {
            "source": "cache",
            "tool": "get_market_landscape",
            "summary_text": (
                f"{resolution.canonical_brand} 기준 같은 시장 전체 매출은 {market_view_label} 기준 "
                f"{eok_value(None, market_size)}입니다."
            ),
            "render_data": data,
        }
        markdown = MarkdownResponseBuilder().build(
            brand=resolution.canonical_brand,
            calls=[call],
            sources=["cache"],
            notices=market_view_notices(view_type),
        )
        return {
            "question": question,
            "resolution": {"canonical_brand": resolution.canonical_brand, "market_id": market_id},
            "decomposition": [{"intent": "same_market_sales", "view_type": view_type}],
            "router_diagnostics": {"deterministic": True},
            "tool_calls": [call],
            "answer": markdown.markdown,
            "markdown_response": markdown.to_dict(),
            "sources": ["cache"],
        }

    def clarification(self, question: str, *, brand: str) -> dict[str, Any]:
        text = (
            "## 답변\n\n"
            f"{brand} 같은 시장 매출은 어느 기준으로 볼까요?\n\n"
            "- ① 전략뷰\n"
            "- ② 경쟁군\n\n"
            "짧게 `전략뷰` 또는 `경쟁군`이라고 답하면 앞 질문에 이어 계산합니다."
        )
        return {
            "question": question,
            "resolution": {"canonical_brand": brand},
            "decomposition": [{"intent": "same_market_sales", "pending": "market_view"}],
            "router_diagnostics": {"deterministic": True},
            "tool_calls": [{"source": "cache", "tool": "market_view_clarification", "render_data": {"brand": brand}}],
            "answer": text,
            "markdown_response": None,
            "sources": ["cache"],
        }

    def unsupported_general_view(self, question: str) -> dict[str, Any]:
        return self._unsupported(
            "일반뷰(atc4) 기준 시장 데이터는 현재 채팅 데이터에 없습니다. 현재는 전략뷰와 경쟁군 기준만 제공합니다.",
            question,
            "view_type",
            "general_view",
        )

    def _view_market_size(
        self,
        *,
        brand: str,
        view_type: str,
        source: str,
        market_id: str,
    ) -> tuple[str | None, float | int | None, float | None]:
        if view_type == "competitive_dynamics":
            series = self._cd_market_size_series(brand=brand, source=source, market_id=market_id)
        else:
            series = self._cause_market_size_series(brand=brand, view_type=view_type, source=source, market_id=market_id)
        if not isinstance(series, dict) or not series:
            return None, None, None
        period = sorted(str(key) for key in series)[-1]
        latest = series.get(period)
        if not isinstance(latest, dict):
            return None, None, None
        value = latest.get("value")
        yoy = latest.get("yoy_growth_pct")
        return period, value if isinstance(value, int | float) else None, yoy if isinstance(yoy, int | float) else None

    def _cd_market_size_series(
        self,
        *,
        brand: str,
        source: str,
        market_id: str,
    ) -> dict[str, Any] | None:
        try:
            return self._cd_mart_cache.snapshot().market_size_series(brand=brand, source=source, market_id=market_id)
        except (LookupError, TypeError):
            return None

    def _cause_market_size_series(
        self,
        *,
        brand: str,
        view_type: str,
        source: str,
        market_id: str,
    ) -> dict[str, Any] | None:
        key = CausePayloadKey(brand=brand, view_type=view_type, source=source, measure="sales", market_id=market_id)
        try:
            payload = self._cause_cache.payload(key).payload
        except (LookupError, TypeError):
            return None
        series = payload.get("data", {}).get("sources_data", {}).get("market_size_series")
        return series if isinstance(series, dict) else None

    @staticmethod
    def _render_data(
        card: dict[str, Any],
        market_id: str,
        source: str,
        period: str,
        market_size: float | int | None,
        yoy: float | None,
        view_type: str,
    ) -> dict[str, Any]:
        card_extended = extended(card)
        market_size_eok = float(market_size) / 100_000_000 if isinstance(market_size, int | float) else None
        return {
            "market": market_id,
            "market_id": market_id,
            "market_name": card.get("market_name") or market_id,
            "anchor_brand": card.get("brand"),
            "scope": "market",
            "scope_label": "시장 전체",
            "metric": "sales",
            "view_type": view_type,
            "view_label": view_label(view_type),
            "period": period,
            "source_label": source,
            "market_size_recent_krw": market_size,
            "market_size_억원": market_size_eok,
            "market_cagr_5y_pct": card_extended.get("market_cagr_5y_pct"),
            "yoy_growth_pct": yoy,
            "brand_sales_krw": card.get("front", {}).get("value_recent"),
        }

    @staticmethod
    def _unsupported(message: str, question: str, field: str, value: str) -> dict[str, Any]:
        call = {
            "source": "cache",
            "tool": "unsupported_metric",
            "summary_text": message,
            "render_data": {
                "status": "unsupported",
                "message": message,
                "unsupported_filters": [{"field": field, "value": value, "reason": message}],
            },
        }
        markdown = MarkdownResponseBuilder().build(brand="시장", calls=[call], sources=["cache"])
        return {
            "question": question,
            "resolution": None,
            "decomposition": [{"intent": "same_market_sales", "status": "unsupported"}],
            "router_diagnostics": {"deterministic": True},
            "tool_calls": [call],
            "answer": markdown.markdown,
            "markdown_response": markdown.to_dict(),
            "sources": ["cache"],
        }
