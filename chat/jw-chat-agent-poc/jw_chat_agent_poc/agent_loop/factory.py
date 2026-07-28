from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any

from jw_chat_agent_poc.agent_loop.loop import ToolUseAgent
from jw_chat_agent_poc.orchestrator.markdown_response import MarkdownResponseBuilder
from jw_chat_agent_poc.orchestrator.unavailable_response import apply_common_unavailable_response
from jw_chat_agent_poc.rag import LocalDocumentRag
from jw_chat_agent_poc.resolver import BrandResolver
from jw_chat_agent_poc.resolver.catalog_membership import TtlCatalogMembershipReader, shared_catalog_membership_reader
from jw_chat_agent_poc.resolver.molecule_reader import TtlBrandMoleculeReader, shared_brand_molecule_reader
from jw_chat_agent_poc.router import BQRouter, BQSubQuestion, LLMFirstBQRouter
from jw_chat_agent_poc.tools.deep_analysis import DeepAnalysisNewsTool
from jw_chat_agent_poc.tools.external import ExternalApiClient
from jw_chat_agent_poc.tools.external.cached_client import (
    CachedExternalApiClient,
    EXTERNAL_RESULT_CACHE_MAX_ENTRIES_ENV,
    EXTERNAL_RESULT_CACHE_TTL_ENV,
)
from jw_chat_agent_poc.tools.external.result_cache import shared_external_result_cache
from jw_chat_agent_poc.tools.metrics import MetricsTool
from jw_chat_agent_poc.tools.query_layer import StrategicQueryLayer


@dataclass(frozen=True, slots=True)
class AgentLoopDependencies:
    metrics: MetricsTool
    resolver: BrandResolver
    news: DeepAnalysisNewsTool
    external: ExternalApiClient
    query_layer: StrategicQueryLayer | None


@dataclass(frozen=True, slots=True)
class ChatAgentDependencies:
    router: BQRouter | LLMFirstBQRouter
    resolver: BrandResolver
    metrics: MetricsTool
    external: ExternalApiClient
    news: DeepAnalysisNewsTool
    rag: LocalDocumentRag
    query_layer: StrategicQueryLayer | None

    def agent_loop_dependencies(self) -> AgentLoopDependencies:
        return AgentLoopDependencies(
            metrics=self.metrics,
            resolver=self.resolver,
            news=self.news,
            external=self.external,
            query_layer=self.query_layer,
        )


@dataclass(frozen=True, slots=True)
class ChatAgentDependencyOverrides:
    router: BQRouter | LLMFirstBQRouter | None = None
    resolver: BrandResolver | None = None
    metrics: MetricsTool | None = None
    external: ExternalApiClient | None = None
    news: DeepAnalysisNewsTool | None = None
    rag: LocalDocumentRag | None = None
    query_layer: StrategicQueryLayer | None = None


def build_agent_loop_dependencies(external_mode: str = "fixture") -> AgentLoopDependencies:
    query_layer = default_query_layer()
    return AgentLoopDependencies(
        metrics=MetricsTool(query_layer=query_layer),
        resolver=BrandResolver(
            membership_reader=default_catalog_membership_reader(),
            molecule_reader=default_brand_molecule_reader(),
        ),
        news=DeepAnalysisNewsTool(),
        external=default_external_client(external_mode),
        query_layer=query_layer,
    )


def build_chat_agent_dependencies(
    *,
    external_mode: str = "fixture",
    overrides: ChatAgentDependencyOverrides | None = None,
) -> ChatAgentDependencies:
    values = overrides or ChatAgentDependencyOverrides()
    query_layer = values.query_layer if values.query_layer is not None else default_query_layer()
    return ChatAgentDependencies(
        router=values.router or LLMFirstBQRouter(),
        resolver=values.resolver or BrandResolver(
            membership_reader=default_catalog_membership_reader(),
            molecule_reader=default_brand_molecule_reader(),
        ),
        metrics=values.metrics or MetricsTool(query_layer=query_layer),
        external=values.external or default_external_client(external_mode),
        news=values.news or DeepAnalysisNewsTool(),
        rag=values.rag or LocalDocumentRag(),
        query_layer=query_layer,
    )


def build_tool_use_agent(dependencies: AgentLoopDependencies) -> ToolUseAgent:
    return ToolUseAgent(
        metrics=dependencies.metrics,
        resolver=dependencies.resolver,
        news=dependencies.news,
        external=dependencies.external,
        query_layer=dependencies.query_layer,
    )


def default_external_client(external_mode: str) -> ExternalApiClient:
    if external_mode != "live":
        return ExternalApiClient(mode=external_mode)
    ttl_seconds = int(os.environ.get(EXTERNAL_RESULT_CACHE_TTL_ENV, "120"))
    max_entries = int(os.environ.get(EXTERNAL_RESULT_CACHE_MAX_ENTRIES_ENV, "256"))
    return CachedExternalApiClient(
        result_cache=shared_external_result_cache(
            ttl_seconds=ttl_seconds,
            max_entries=max_entries,
        ),
    )


def default_query_layer() -> StrategicQueryLayer | None:
    enabled = os.environ.get("CHAT_QUERY_LAYER_ENABLED", "1").lower() not in {"0", "false", "no"}
    if not enabled:
        return None
    if os.environ.get("CHAT_METRICS_MODE", "fixture") != "cache":
        return None
    return StrategicQueryLayer()


def default_catalog_membership_reader() -> TtlCatalogMembershipReader | None:
    if os.environ.get("CHAT_METRICS_MODE", "fixture") != "cache":
        return None
    ttl_seconds = int(os.environ.get("CHAT_RESOLVER_TTL_SECONDS", "300"))
    return shared_catalog_membership_reader(ttl_seconds)


def default_brand_molecule_reader() -> TtlBrandMoleculeReader | None:
    if os.environ.get("CHAT_METRICS_MODE", "fixture") != "cache":
        return None
    ttl_seconds = int(os.environ.get("CHAT_RESOLVER_TTL_SECONDS", "300"))
    return shared_brand_molecule_reader(ttl_seconds)


def unsupported_brand_result(
    question: str,
    routes: list[BQSubQuestion],
    diagnostics: dict[str, Any],
) -> dict[str, Any]:
    markdown = MarkdownResponseBuilder().unsupported_brand(
        "요청한 이름과 일치하는 브랜드가 확인되지 않습니다. 브랜드명을 확인해 주세요."
    )
    return {
        "question": question,
        "resolution": None,
        "decomposition": [route.__dict__ for route in routes],
        "router_diagnostics": diagnostics,
        "tool_calls": [],
        "answer": markdown.markdown,
        "markdown_response": markdown.to_dict(),
        "sources": ["unsupported_brand"],
    }


def brand_unresolved_result(
    question: str,
    routes: list[BQSubQuestion] | tuple[BQSubQuestion, ...],
    diagnostics: dict[str, Any],
) -> dict[str, Any]:
    """Ask which brand was meant, instead of letting the planner's exception out.

    Distinct from ``unsupported_brand_result``: that one tells the user the name
    they gave is not a known brand, which would be wrong here — the question may
    name a disease or a market and never claim to name a brand at all. The
    wording carries no digits so the notice surface cannot drop it, and
    ``gate_reason`` is set so the recovery is visible in qa_trace rather than
    only in pod logs.
    """

    markdown = MarkdownResponseBuilder().brand_unresolved(
        "어느 브랜드 기준인지 확인되지 않아 답변을 드릴 수 없습니다. "
        "브랜드명을 함께 알려주시거나, 시장 단위로 보시려면 시장을 지정해 주세요."
    )
    return {
        "question": question,
        "resolution": None,
        "decomposition": [route.__dict__ for route in routes],
        "router_diagnostics": {**diagnostics, "gate_reason": "brand_unresolved"},
        "tool_calls": [],
        "answer": markdown.markdown,
        "markdown_response": markdown.to_dict(),
        "sources": ["brand_unresolved"],
        "brand_unresolved": True,
    }


def ambiguous_brand_result(
    question: str,
    routes: list[BQSubQuestion] | tuple[BQSubQuestion, ...],
    diagnostics: dict[str, Any],
    candidates: tuple[str, ...],
) -> dict[str, Any]:
    candidate_text = ", ".join(candidates)
    markdown = MarkdownResponseBuilder().ambiguous_brand(
        f"요청한 이름만으로 하나의 브랜드를 정할 수 없습니다. 후보: {candidate_text}. 하나를 지정해 주세요."
    )
    return {
        "question": question,
        "resolution": {"status": "ambiguous", "candidates": list(candidates)},
        "decomposition": [route.__dict__ for route in routes],
        "router_diagnostics": diagnostics,
        "tool_calls": [],
        "answer": markdown.markdown,
        "markdown_response": markdown.to_dict(),
        "sources": ["ambiguous_brand"],
    }


def unsupported_hira_interface_result(
    question: str,
    routes: list[BQSubQuestion],
    diagnostics: dict[str, Any],
) -> dict[str, Any]:
    markdown = MarkdownResponseBuilder().unsupported_brand(
        "현재 HIRA 조회는 브랜드 기준으로만 지원됩니다. "
        "상병코드 또는 질환명 직접 조회는 현재 인터페이스에서 처리할 수 없습니다. "
        "다른 대상의 통계를 대신 반환하지 않으며, 상병코드 기준 통계는 "
        "HIRA 보건의료빅데이터개방시스템에서 확인해 주세요."
    )
    return {
        "question": question,
        "resolution": None,
        "decomposition": [route.__dict__ for route in routes],
        "router_diagnostics": diagnostics,
        "tool_calls": [],
        "answer": markdown.markdown,
        "markdown_response": markdown.to_dict(),
        "sources": ["unsupported_hira_interface"],
    }


def field_not_exposed_result(
    question: str,
    _capability: str,
    routes: list[BQSubQuestion],
    diagnostics: dict[str, Any],
) -> dict[str, Any]:
    markdown = MarkdownResponseBuilder().field_not_exposed(
        "요청한 상세 항목은 현재 연결에서 제공되지 않습니다. "
        "확인 가능한 다른 허가 항목을 지정해 주세요."
    )
    return {
        "question": question,
        "resolution": None,
        "decomposition": [route.__dict__ for route in routes],
        "router_diagnostics": diagnostics,
        "tool_calls": [],
        "answer": markdown.markdown,
        "markdown_response": markdown.to_dict(),
        "sources": ["field_not_exposed"],
    }


PRESCRIPTION_METRIC_UNAVAILABLE_FACT_MD = "현재 채팅 조회 계약에서 처방 지표 미지원"
PRESCRIPTION_METRIC_UNAVAILABLE_REASON = (
    "요청한 처방 지표는 현재 채팅 조회 계약에 미노출되어 확인할 수 없습니다. "
    "값은 null로 반환하며 매출 지표로 대체하지 않습니다."
)


def prescription_metric_unavailable_result(
    question: str,
    requested_metric: str,
    diagnostics: dict[str, Any],
) -> dict[str, Any]:
    reason = PRESCRIPTION_METRIC_UNAVAILABLE_REASON
    markdown = MarkdownResponseBuilder().field_not_exposed(reason)
    markdown_payload = markdown.to_dict()
    markdown_payload["fact_md"] = "현재 채팅 조회 계약에서 처방 지표 미지원"
    answer = apply_common_unavailable_response(
        question,
        markdown.markdown,
        markdown_payload,
    )
    return {
        "question": question,
        "resolution": None,
        "decomposition": [
            {
                "intent": "prescription_metric",
                "metric": requested_metric,
                "status": "unavailable",
                "reason_code": "FIELD_NOT_EXPOSED",
            }
        ],
        "router_diagnostics": diagnostics,
        "tool_calls": [],
        "answer": answer,
        "markdown_response": markdown_payload,
        "sources": ["field_not_exposed"],
        "status": "unavailable",
        "reason_code": "FIELD_NOT_EXPOSED",
        "value": None,
        "reason": "prescription_metric_not_exposed",
        "proxy": {
            "metric": "sales",
            "status": "separate_request_only",
            "substituted": False,
        },
    }
