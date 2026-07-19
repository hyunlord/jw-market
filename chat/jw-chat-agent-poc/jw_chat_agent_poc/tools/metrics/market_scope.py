from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Any

from jw_chat_agent_poc.common.qa_trace import attach_tool_qa_trace, qa_trace_started_at
from jw_chat_agent_poc.orchestrator.markdown_formatting import eok_value
from jw_chat_agent_poc.orchestrator.markdown_response import MarkdownResponseBuilder
from jw_chat_agent_poc.resolver import BrandResolver, UnsupportedBrandError
from jw_chat_agent_poc.resolver.brand_resolver import BrandMembershipReader
from jw_chat_agent_poc.resolver.catalog_membership import shared_catalog_membership_reader
from jw_chat_agent_poc.service.general_view_routing import GeneralRoute, GeneralViewService
from jw_chat_agent_poc.tools.metrics.cache_live import (
    MetricsCacheReader,
    TtlMetricsCache,
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
from jw_chat_agent_poc.tools.cause_backend import CauseBackend, CauseBackendError
from jw_chat_agent_poc.tools.query_layer import StrategicQueryLayer


class MarketScopeResolver:
    def __init__(
        self,
        *,
        cache_reader: MetricsCacheReader | None = None,
        cause_reader: object | None = None,
        cd_mart_reader: CdMartReader | None = None,
        ttl_seconds: int | None = None,
        general_view_service: GeneralViewService | None = None,
        membership_reader: BrandMembershipReader | None = None,
        query_layer: StrategicQueryLayer | None = None,
    ) -> None:
        ttl = ttl_seconds or int(os.environ.get("CHAT_MARKET_SCOPE_TTL_SECONDS", "300"))
        self._cache = TtlMetricsCache(cache_reader, ttl_seconds=ttl) if cache_reader is not None else shared_metrics_cache(ttl)
        self._cd_mart_cache = TtlCdMartCache(cd_mart_reader or MariaDbCdMartReader(), ttl_seconds=ttl)
        self._query_layer = query_layer
        if self._query_layer is None and cache_reader is None and cause_reader is None:
            if os.environ.get("CHAT_METRICS_MODE", "fixture") == "cache":
                self._query_layer = StrategicQueryLayer(
                    ttl_seconds=ttl,
                    cause_backend=CauseBackend(ttl_seconds=ttl),
                )
        catalog_membership = membership_reader
        if catalog_membership is None and os.environ.get("CHAT_METRICS_MODE", "fixture") == "cache":
            catalog_membership = shared_catalog_membership_reader(ttl)
        self._resolver = BrandResolver(
            mode="cache",
            brand_reader=cache_reader,
            membership_reader=catalog_membership,
            ttl_seconds=ttl,
        )
        self._general_view = general_view_service or GeneralViewService.from_env(self._resolver)

    def general_route(self, question: str) -> GeneralRoute:
        return self._general_view.route(question)

    def has_explicit_anchor(self, question: str) -> bool:
        if re.search(r"(?<![A-Za-z0-9_])ml_\d+(?![A-Za-z0-9_])", question, re.IGNORECASE):
            return True
        if re.search(r"(?<![A-Za-z0-9])(?:[A-Z]\d{2}[A-Z]\d)(?![A-Za-z0-9])", question, re.IGNORECASE):
            return True
        return self._resolver.has_explicit_alias(question)

    def has_explicit_brand_anchor(self, question: str) -> bool:
        return self._resolver.has_explicit_alias(question)

    def answer_general(self, question: str, *, compact: bool, dual: bool) -> dict[str, Any]:
        return self._general_view.answer(question, compact=compact, dual=dual)

    def answer(self, question: str, *, view_type: MarketView) -> dict[str, Any]:
        started_at = qa_trace_started_at()
        if view_type == "general_view":
            return self.answer_general(question, compact=False, dual=False)
        try:
            resolution = self._resolver.resolve(question, allow_default=False)
            if self._query_layer is not None:
                return self._query_layer_answer(
                    question,
                    resolution.canonical_brand,
                    view_type,
                    started_at=started_at,
                )
            snapshot = self._cache.snapshot()
            card = find_brand_card(snapshot.market_status, resolution.canonical_brand)
        except (LookupError, UnsupportedBrandError) as exc:
            return self._unsupported(str(exc), question, "brand", "unknown")

        market_id = str(card.get("market_id") or resolution.market_id or "")
        source = source_label(card, find_brand_bridge(snapshot.cache_brands, resolution.canonical_brand))
        if view_type == "market_landscape":
            period = period_recent(snapshot.market_status, card) or str(extended(card).get("period_recent") or "latest")
            market_size = extended(card).get("market_size_recent")
            yoy = extended(card).get("yoy_growth_pct")
        else:
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
        attach_tool_qa_trace(call, started_at=started_at, cache_hit=True)
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
            "router_diagnostics": {
                "deterministic": True,
                "mode": "market_scope",
                "scope": view_type,
                "gate": "metric_owner",
                "gate_reason": "market_scope",
            },
            "tool_calls": [call],
            "answer": markdown.markdown,
            "markdown_response": markdown.to_dict(),
            "sources": ["cache"],
        }

    def answer_monthly_market_golden(
        self,
        question: str,
        *,
        anchor_brand: str,
    ) -> dict[str, Any]:
        """Preserve the approved monthly P0-2 truth for synthetic market anchors."""

        started_at = qa_trace_started_at()
        if self._query_layer is None:
            return self._unsupported(
                "전략 시장 조회 계층을 사용할 수 없습니다.",
                question,
                "brand",
                anchor_brand,
            )
        try:
            resolution = self._resolver.resolve(anchor_brand, allow_default=False)
        except (LookupError, UnsupportedBrandError) as exc:
            return self._unsupported(str(exc), question, "brand", anchor_brand)
        return self._query_layer_answer(
            question,
            resolution.canonical_brand,
            "market_landscape",
            started_at=started_at,
            use_mart=True,
        )

    def answer_market_id(self, question: str, *, market_id: str, period: str = "latest") -> dict[str, Any]:
        started_at = qa_trace_started_at()
        if self._query_layer is None:
            return self._unsupported("전략 시장 조회 계층을 사용할 수 없습니다.", question, "market_id", market_id)
        try:
            call = self._query_layer.market_scope_by_id(market_id, period)
        except (LookupError, TypeError, ValueError) as exc:
            return self._unsupported(str(exc), question, "market_id", market_id)
        data = call.get("render_data")
        if not isinstance(data, dict):
            return self._unsupported("전략 mart 응답 구조가 비어 있습니다.", question, "market_id", market_id)
        source = str(data.get("source_label") or call.get("source") or "")
        attach_tool_qa_trace(call, started_at=started_at, cache_hit=False)
        markdown = MarkdownResponseBuilder().build(
            brand="해당 전략 시장",
            calls=[call],
            sources=[source],
            notices=market_view_notices("market_landscape"),
        )
        return {
            "question": question,
            "resolution": {"market_id": market_id},
            "decomposition": [{"intent": "market_size", "view_type": "market_landscape", "period": period}],
            "router_diagnostics": {
                "deterministic": True,
                "mode": "market_scope",
                "scope": "market_landscape",
                "gate": "metric_owner",
                "gate_reason": "explicit_market_id",
                "explicit_market_id": True,
            },
            "tool_calls": [call],
            "answer": markdown.markdown,
            "markdown_response": markdown.to_dict(),
            "sources": [source],
        }

    def _query_layer_answer(
        self,
        question: str,
        brand: str,
        view_type: MarketView,
        *,
        started_at: datetime,
        use_mart: bool = False,
    ) -> dict[str, Any]:
        assert self._query_layer is not None
        try:
            call = (
                self._query_layer.market_scope_from_mart(brand)
                if view_type == "competitive_dynamics" or use_mart
                else self._query_layer.market_scope(brand)
            )
        except (LookupError, TypeError, ValueError) as exc:
            return self._query_failed(exc, question, "get_market_scope", brand, started_at=started_at)

        data = call.get("render_data")
        if not isinstance(data, dict):
            return self._unsupported("전략 mart 응답 구조가 비어 있습니다.", question, "brand", brand)
        market_reference = str(data.get("market_id") or data.get("market") or "")
        market_name = str(data.get("market_name") or market_reference)
        source = str(data.get("source_label") or call.get("source") or "")
        if view_type == "competitive_dynamics":
            period, market_size, yoy = self._view_market_size(
                brand=brand,
                view_type=view_type,
                source=source,
                market_id=market_reference,
            )
            if market_size is None:
                return self._unsupported(
                    "경쟁군 시장규모를 전략 CD mart에서 찾지 못했습니다.",
                    question,
                    "view_type",
                    view_type,
                )
            data = dict(data)
            data.update(
                {
                    "period": period,
                    "market_size_recent_krw": market_size,
                    "market_size_억원": float(market_size) / 100_000_000,
                    "yoy_growth_pct": yoy,
                }
            )
        data["view_type"] = view_type
        data["view_label"] = view_label(view_type)
        call["render_data"] = data
        call["summary_text"] = (
            f"{brand} 기준 같은 시장 전체 매출은 {view_label(view_type)} 기준 "
            f"{eok_value(None, data.get('market_size_recent_krw'))}입니다."
        )
        attach_tool_qa_trace(
            call,
            started_at=started_at,
            cache_hit=False if view_type == "competitive_dynamics" or use_mart else None,
        )
        markdown = MarkdownResponseBuilder().build(
            brand=brand,
            calls=[call],
            sources=[source],
            notices=market_view_notices(view_type),
        )
        resolution: dict[str, str] = {"canonical_brand": brand, "market_name": market_name}
        if view_type == "competitive_dynamics" and market_reference:
            resolution["market_id"] = market_reference
        return {
            "question": question,
            "resolution": resolution,
            "decomposition": [{"intent": "same_market_sales", "view_type": view_type}],
            "router_diagnostics": {
                "deterministic": True,
                "mode": "market_scope",
                "scope": view_type,
                "gate": "metric_owner",
                "gate_reason": "monthly_market_golden" if use_mart else "market_scope",
            },
            "tool_calls": [call],
            "answer": markdown.markdown,
            "markdown_response": markdown.to_dict(),
            "sources": [source],
        }

    @staticmethod
    def _query_failed(
        error: BaseException,
        question: str,
        tool_name: str,
        brand: str,
        *,
        started_at: datetime,
    ) -> dict[str, Any]:
        message = "요청한 시장 조회 실행이 실패했습니다. 데이터가 없다는 뜻은 아니며, 수치를 추정하지 않습니다."
        render_data: dict[str, Any] = {
            "status": "query_failed",
            "message": message,
            "tool_name": tool_name,
            "brand": brand,
            "error_type": type(error).__name__,
        }
        call: dict[str, Any] = {
            "source": "backend_api",
            "tool": "query_failed",
            "status": "query_failed",
            "summary_text": message,
            "render_data": render_data,
        }
        if isinstance(error, CauseBackendError):
            call["backend_trace"] = error.trace_fields()
        trace_status = error.status if isinstance(error, CauseBackendError) else "query_failed"
        attach_tool_qa_trace(call, started_at=started_at, status=trace_status, cache_hit=False)
        markdown = MarkdownResponseBuilder().build(brand=brand, calls=[call], sources=["backend_api"])
        return {
            "question": question,
            "resolution": {"canonical_brand": brand},
            "decomposition": [{"intent": "same_market_sales", "status": "query_failed"}],
            "router_diagnostics": {
                "deterministic": True,
                "mode": "market_scope",
                "scope": "market_landscape",
                "gate": "typed_unavailable",
                "gate_reason": "query_failed",
            },
            "tool_calls": [call],
            "answer": markdown.markdown,
            "markdown_response": markdown.to_dict(),
            "sources": ["backend_api"],
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

    def _view_market_size(
        self,
        *,
        brand: str,
        view_type: str,
        source: str,
        market_id: str,
    ) -> tuple[str | None, float | int | None, float | None]:
        if view_type != "competitive_dynamics":
            return None, None, None
        series = self._cd_market_size_series(brand=brand, source=source, market_id=market_id)
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
