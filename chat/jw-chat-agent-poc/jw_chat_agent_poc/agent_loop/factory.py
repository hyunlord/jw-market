from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any

from jw_chat_agent_poc.agent_loop.loop import ToolUseAgent
from jw_chat_agent_poc.orchestrator.markdown_response import MarkdownResponseBuilder
from jw_chat_agent_poc.rag import LocalDocumentRag
from jw_chat_agent_poc.resolver import BrandResolver
from jw_chat_agent_poc.resolver.catalog_membership import TtlCatalogMembershipReader, shared_catalog_membership_reader
from jw_chat_agent_poc.resolver.molecule_reader import TtlBrandMoleculeReader, shared_brand_molecule_reader
from jw_chat_agent_poc.router import BQRouter, BQSubQuestion, LLMFirstBQRouter
from jw_chat_agent_poc.tools.deep_analysis import DeepAnalysisNewsTool
from jw_chat_agent_poc.tools.cause_backend import CauseBackend
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
    return StrategicQueryLayer(cause_backend=CauseBackend())


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
        "요청한 브랜드는 현재 전략 마트 원천에서 확인되지 않습니다. 브랜드명을 확인해 주세요."
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
