from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Any

from jw_chat_agent_poc.common.periods import requested_period
from jw_chat_agent_poc.common.qa_trace import attach_tool_qa_trace, qa_trace_started_at
from jw_chat_agent_poc.orchestrator.markdown_formatting import eok_value
from jw_chat_agent_poc.orchestrator.markdown_response import MarkdownResponseBuilder
from jw_chat_agent_poc.resolver import BrandResolution, BrandResolver, UnsupportedBrandError
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
    asks_market_members,
    detect_market_scope_intent,
    map_market_view_reply,
    requested_market_member_limit,
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
from jw_chat_agent_poc.tools.query_layer import StrategicQueryLayer


def _strategic_cause_result(
    question: str,
    call: dict[str, Any],
    *,
    market_name: str,
    brand: str = "",
) -> dict[str, Any]:
    data = dict(call.get("render_data") or {})
    segments = [dict(row) for row in data.get("level_segments", ()) if isinstance(row, dict)]
    tables: list[dict[str, Any]] = [
        {
            "name": "시장 핵심 지표",
            "columns": ("기준시점", "시장 규모(억원)", "HHI"),
            "rows": (
                (
                    data.get("period"),
                    data.get("market_size_억원"),
                    data.get("hhi_recent"),
                ),
            ),
        }
    ]
    if segments:
        tables.append(
            {
                "name": "브랜드 순위",
                "columns": ("순위", "브랜드", "매출(억원)", "점유율(%)"),
                "rows": tuple(
                    (
                        row.get("rank"),
                        row.get("brand") or row.get("name"),
                        row.get("value_억원"),
                        row.get("ms_recent_pct"),
                    )
                    for row in segments[:10]
                ),
            }
        )
    charts = []
    chart_segments = [
        (index, row)
        for index, row in enumerate(segments)
        if eok_value(row.get("value_억원"), None)
    ][:5]
    if chart_segments:
        charts.append(
            {
                "scope": "MARKET",
                "chart_type": "bar",
                "title": f"{market_name} 브랜드 순위",
                "labels": [row.get("brand") or row.get("name") for _, row in chart_segments],
                "datasets": [
                    {
                        "label": "매출",
                        "data": [row.get("value_억원") for _, row in chart_segments],
                        "unit": "억원",
                    }
                ],
                "source": str(data.get("source_label") or call.get("source") or "전략 mart"),
                "unit": "억원",
                "evidence_refs": [
                    f"render_data.level_segments[{index}].value_억원"
                    for index, _ in chart_segments
                ],
            }
        )
    data.update(
        {
            "dashboard_tables": tables,
            "chart_payloads": charts,
            "cause_card_support": {
                "A1_market_size_growth": data.get("market_size_recent_krw") is not None,
                "A2_brand_ranking": bool(segments),
                "A3_hhi": data.get("hhi_recent") is not None,
                "A4_company_ranking": False,
                "A5_company_concentration": False,
                "B1_ei_ms": False,
                "B2_growth_contribution_ms": False,
                "C1_analysis_level_trend": False,
                "D1_waterfall": False,
                "D2_customer_competition": False,
                "D3_level_top5": bool(segments),
            },
        }
    )
    call = {**call, "render_data": data}
    lines = ["## 원인분석", "", f"- 시장: {market_name}", f"- 기준시점: {data.get('period') or '확인 불가'}"]
    for table in tables:
        lines.extend(("", f"### {table['name']}", "", "| " + " | ".join(table["columns"]) + " |"))
        lines.append("| " + " | ".join("---" for _ in table["columns"]) + " |")
        for row in table["rows"]:
            lines.append("| " + " | ".join(_cause_cell(value) for value in row) + " |")
    resolution = {"market_id": str(data.get("market_id") or data.get("market") or ""), "market_name": market_name}
    if brand:
        resolution["canonical_brand"] = brand
    return {
        "question": question,
        "resolution": resolution,
        "decomposition": [{"intent": "cause_analysis", "view_type": "market_landscape"}],
        "router_diagnostics": {
            "deterministic": True,
            "mode": "cause_analysis",
            "scope": "market_landscape",
            "reason": "strategic_market_direct_mart",
        },
        "tool_calls": [call],
        "answer": "\n".join(lines),
        "markdown_response": None,
        "sources": [str(data.get("source_label") or call.get("source") or "전략 mart")],
        "cause_analysis_ready": True,
    }


def _cause_cell(value: object) -> str:
    if isinstance(value, float):
        return f"{value:,.2f}"
    if isinstance(value, int):
        return f"{value:,}"
    if value is None:
        return "확인 불가"
    return str(value)


class MarketScopeResolver:
    def __init__(
        self,
        *,
        cache_reader: MetricsCacheReader | None = None,
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
        if self._query_layer is None and cache_reader is None:
            if os.environ.get("CHAT_METRICS_MODE", "fixture") == "cache":
                self._query_layer = StrategicQueryLayer(ttl_seconds=ttl)
        catalog_membership = membership_reader
        if catalog_membership is None and os.environ.get("CHAT_METRICS_MODE", "fixture") == "cache":
            catalog_membership = shared_catalog_membership_reader(ttl)
        self._catalog_membership = catalog_membership
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

    def resolve_many(
        self,
        question_or_brands: str,
        allow_default: bool = False,
    ) -> tuple[BrandResolution, ...]:
        return self._resolver.resolve_many(
            question_or_brands,
            allow_default=allow_default,
        )

    def is_general_only_brand(self, question: str) -> bool:
        try:
            resolution = self._resolver.resolve(question, allow_default=False)
        except (LookupError, OSError, TypeError, ValueError):
            return False
        return not bool(resolution.market_ids or resolution.market_id)

    def has_explicit_named_market(self, question: str) -> bool:
        try:
            return self._resolver.explicit_market(question) is not None
        except Exception:  # noqa: BLE001 - an unavailable catalog must not block unrelated routing
            return False

    def runtime_observability(self) -> dict[str, dict[str, Any]]:
        strategic = (
            self._query_layer.observability()
            if self._query_layer is not None
            else {
                "row_count": 0,
                "derived_point_count": 0,
                "market_point_count": 0,
                "brand_point_count": 0,
                "snapshot_age_seconds": None,
                "refresh_successes": 0,
                "refresh_failures": 0,
                "refreshing": False,
            }
        )
        return {
            "strategic_mart": strategic,
            "catalog": self._resolver.observability(),
            "general_membership": self._general_view.observability(),
        }

    def answer_general(self, question: str, *, compact: bool, dual: bool) -> dict[str, Any]:
        return self._general_view.answer(question, compact=compact, dual=dual)

    def answer_cause_analysis(self, question: str) -> dict[str, Any]:
        """Resolve the requested market, then read its approved mart projection.

        Strategic catalog membership owns strategic markets. Brands outside that
        catalog stay on the existing dynamic ATC4 path; both branches read their
        approved mart projection instead of a retired cache or HTTP cause route.
        """

        started_at = qa_trace_started_at()
        if self._query_layer is not None:
            try:
                explicit_market = self._resolver.explicit_market(question)
            except (LookupError, OSError, TypeError, ValueError):
                explicit_market = None
            if explicit_market is not None:
                market_id, market_name = explicit_market
                try:
                    call = self._query_layer.market_scope_by_id(
                        market_id,
                        requested_period(question) or "latest",
                        market_display_name=market_name,
                    )
                except (LookupError, TypeError, ValueError) as exc:
                    return self._query_failed(
                        exc,
                        question,
                        "get_market_landscape",
                        market_name,
                        started_at=qa_trace_started_at(),
                    )
                attach_tool_qa_trace(call, started_at=started_at, cache_hit=False)
                return _strategic_cause_result(question, call, market_name=market_name)
            try:
                resolution = self._resolver.resolve(question, allow_default=False)
            except (LookupError, UnsupportedBrandError, OSError, TypeError, ValueError):
                resolution = None
            if resolution is not None and (resolution.market_ids or resolution.market_id):
                try:
                    call = self._query_layer.market_scope_from_mart(resolution.canonical_brand)
                except (LookupError, TypeError, ValueError) as exc:
                    return self._query_failed(
                        exc,
                        question,
                        "get_market_landscape",
                        resolution.canonical_brand,
                        started_at=qa_trace_started_at(),
                    )
                attach_tool_qa_trace(call, started_at=started_at, cache_hit=False)
                return _strategic_cause_result(
                    question,
                    call,
                    market_name=resolution.market_name or "해당 전략 시장",
                    brand=resolution.canonical_brand,
                )
        result = self.answer_general(question, compact=False, dual=False)
        result["cause_analysis_ready"] = not bool(
            (result.get("general_view_contract") or {}).get("unavailable")
        )
        return result

    def answer(self, question: str, *, view_type: MarketView) -> dict[str, Any]:
        started_at = qa_trace_started_at()
        if view_type == "general_view":
            return self.answer_general(question, compact=False, dual=False)
        try:
            resolution = self._resolver.resolve(question, allow_default=False)
            if not resolution.market_ids:
                return self._strategic_market_unavailable(question, resolution.canonical_brand)
            if self._query_layer is not None:
                return self._query_layer_answer(
                    question,
                    resolution.canonical_brand,
                    view_type,
                    started_at=started_at,
                    market_display_name=resolution.market_name,
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

    def answer_named_market(self, question: str) -> dict[str, Any]:
        started_at = qa_trace_started_at()
        explicit_market = self._resolver.explicit_market(question)
        if explicit_market is None:
            return self._unsupported("전략시장 이름을 해소할 수 없습니다.", question, "market", "unknown")
        market_id, market_name = explicit_market
        return self._query_layer_answer(
            question,
            "",
            "market_landscape",
            started_at=started_at,
            market_id=market_id,
            market_display_name=market_name,
        )

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
            market_display_name=resolution.market_name,
        )

    def _catalog_market_label(self, market_id: str) -> str | None:
        """The catalog's public name for a market, or None when only the id is known.

        Reads through BrandMembershipReader.brand_memberships(), the contract this
        resolver is handed (brand_resolver.py). The lookalike CatalogMembershipSource
        .load() belongs to the raw source that TtlCatalogMembershipReader wraps —
        calling it here is what broke deploy 28, so the reader contract is used and
        failures are left to propagate rather than being masked into a missing label.

        catalog_membership COALESCEs a NULL catalog name to the ml_id, so a row whose
        market_name is just the identifier carries no public label and yields None.
        """
        reader = self._catalog_membership
        if reader is None:
            return None
        target = market_id.strip().casefold()
        for row in reader.brand_memberships():
            if str(row.get("market_id") or "").strip().casefold() != target:
                continue
            name = str(row.get("market_name") or "").strip()
            if name and name.casefold() != target:
                return name
        return None

    def answer_market_id(
        self,
        question: str,
        *,
        market_id: str,
        period: str = "latest",
        market_display_name: str | None = None,
    ) -> dict[str, Any]:
        started_at = qa_trace_started_at()
        if self._query_layer is None:
            return self._unsupported("전략 시장 조회 계층을 사용할 수 없습니다.", question, "market_id", market_id)
        display_name = (market_display_name or "").strip() or self._catalog_market_label(market_id)
        member_query = asks_market_members(question)
        member_limit = requested_market_member_limit(question)
        try:
            call = (
                self._query_layer.market_members(market=market_id, period=period, limit=member_limit.applied)
                if member_query
                else self._query_layer.market_scope_by_id(
                    market_id, period, market_display_name=display_name
                )
            )
        except LookupError as exc:
            if member_query and self._is_market_members_unavailable(exc):
                return self._market_members_unavailable(question, exc, "market_id", market_id)
            return self._unsupported(str(exc), question, "market_id", market_id)
        except (TypeError, ValueError) as exc:
            return self._unsupported(str(exc), question, "market_id", market_id)
        data = call.get("render_data")
        if not isinstance(data, dict):
            return self._unsupported("전략 mart 응답 구조가 비어 있습니다.", question, "market_id", market_id)
        if member_query and (
            (member_limit.requested is not None and member_limit.requested > 0)
            or member_limit.all_requested
        ):
            if member_limit.requested is not None and member_limit.requested > 0:
                data["requested_limit"] = member_limit.requested
            else:
                data["requested_all"] = True
            data["display_limit"] = int(data.get("displayed_brand_count") or 0)
            data["limit_capped"] = False
            call["render_data"] = data
        source = str(data.get("source_label") or call.get("source") or "")
        attach_tool_qa_trace(call, started_at=started_at, cache_hit=False)
        markdown = MarkdownResponseBuilder().build(
            brand=display_name or "해당 전략 시장",
            calls=[call],
            sources=[source],
            notices=market_view_notices("market_landscape"),
        )
        return {
            "question": question,
            "resolution": {"market_id": market_id},
            "decomposition": [
                {
                    "intent": "market_members" if asks_market_members(question) else "market_size",
                    "view_type": "market_landscape",
                    "period": period,
                }
            ],
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
        market_id: str | None = None,
        market_display_name: str | None = None,
    ) -> dict[str, Any]:
        assert self._query_layer is not None
        member_query = asks_market_members(question)
        member_limit = requested_market_member_limit(question)
        try:
            if member_query:
                call = self._query_layer.market_members(
                    brand,
                    market=market_id,
                    period=requested_period(question) or "latest",
                    limit=member_limit.applied,
                    include_other="기타" in question,
                )
            else:
                call = (
                    self._query_layer.market_scope_from_mart(brand)
                    if view_type == "competitive_dynamics" or use_mart
                    else self._query_layer.market_scope(brand)
                )
        except LookupError as exc:
            if member_query and self._is_market_members_unavailable(exc):
                field = "market_id" if market_id else "brand"
                value = market_id or brand
                return self._market_members_unavailable(question, exc, field, value)
            return self._query_failed(exc, question, "get_market_scope", brand, started_at=started_at)
        except (TypeError, ValueError) as exc:
            return self._query_failed(exc, question, "get_market_scope", brand, started_at=started_at)

        data = call.get("render_data")
        if not isinstance(data, dict):
            return self._unsupported("전략 mart 응답 구조가 비어 있습니다.", question, "brand", brand)
        market_reference = str(data.get("market_id") or data.get("market") or "")
        market_name = str(market_display_name or data.get("market_name") or market_reference)
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
        data["market_name"] = market_name
        data["view_type"] = view_type
        data["view_label"] = view_label(view_type)
        if member_query and (
            (member_limit.requested is not None and member_limit.requested > 0)
            or member_limit.all_requested
        ):
            if member_limit.requested is not None and member_limit.requested > 0:
                data["requested_limit"] = member_limit.requested
            else:
                data["requested_all"] = True
            data["display_limit"] = int(data.get("displayed_brand_count") or 0)
            data["limit_capped"] = False
        call["render_data"] = data
        if member_query:
            qualifier = "상위 5개 밖의 " if data.get("other_members_only") else ""
            market_subject = market_name if market_name.endswith("시장") else f"{market_name} 시장"
            call["summary_text"] = (
                f"{market_subject}의 {qualifier}구성 브랜드를 전략 mart에서 조회했습니다. "
                f"총 {int(data.get('total_brands_in_market') or 0):,}개 중 "
                f"{int(data.get('displayed_brand_count') or 0):,}개 표시"
            )
        else:
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
            brand=brand or market_name,
            calls=[call],
            sources=[source],
            notices=market_view_notices(view_type),
        )
        resolution: dict[str, str] = {"market_name": market_name}
        if brand:
            resolution["canonical_brand"] = brand
        if market_reference:
            resolution["market_id"] = market_reference
        return {
            "question": question,
            "resolution": resolution,
            "decomposition": [
                {
                    "intent": "market_members" if asks_market_members(question) else "same_market_sales",
                    "view_type": view_type,
                }
            ],
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
        attach_tool_qa_trace(call, started_at=started_at, status="query_failed", cache_hit=False)
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
    def _strategic_market_unavailable(question: str, brand: str) -> dict[str, Any]:
        message = "이 브랜드는 전략시장 정의에 포함되지 않아 해당 분석은 제공되지 않습니다."
        result = MarketScopeResolver._unsupported(message, question, "brand", brand)
        result["resolution"] = {
            "canonical_brand": brand,
            "market_membership": "general_only",
        }
        result["router_diagnostics"] = {
            "deterministic": True,
            "mode": "market_scope",
            "scope": "market_landscape",
            "gate": "typed_unavailable",
            "gate_reason": "strategic_market_not_member",
        }
        result["sources"] = ["strategic_market_not_member"]
        return result

    @staticmethod
    def _market_members_unavailable(
        question: str,
        error: LookupError,
        field: str,
        value: str,
    ) -> dict[str, Any]:
        if "market not found" in str(error).lower():
            message = "시장 매핑이 확인되지 않습니다."
            reason = "market_members_mapping_unavailable"
        else:
            message = "이 시장은 구성원 정보를 제공하지 않습니다."
            reason = "market_members_data_unavailable"
        result = MarketScopeResolver._unsupported(message, question, field, value)
        result["decomposition"] = [{"intent": "market_members", "status": "unsupported"}]
        result["router_diagnostics"] = {
            "deterministic": True,
            "mode": "market_scope",
            "scope": "market_landscape",
            "gate": "typed_unavailable",
            "gate_reason": reason,
        }
        result["sources"] = [reason]
        return result

    @staticmethod
    def _is_market_members_unavailable(error: LookupError) -> bool:
        detail = str(error).lower()
        return "mart market not found:" in detail or "market member data" in detail

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
