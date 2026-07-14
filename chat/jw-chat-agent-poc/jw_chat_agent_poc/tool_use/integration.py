from __future__ import annotations

import logging
import os
from typing import Any, Final

from jw_chat_agent_poc.resolver import BrandResolver
from jw_chat_agent_poc.tool_use.contracts import AgentResult
from jw_chat_agent_poc.tool_use.executor import AgentExecutor
from jw_chat_agent_poc.tool_use.provider import GenosToolChoiceProvider, ToolChoiceProvider
from jw_chat_agent_poc.tool_use.registry import ExternalToolRegistry
from jw_chat_agent_poc.tools.external import ExternalApiClient


LOGGER = logging.getLogger(__name__)
FEATURE_FLAG: Final[str] = "CHAT_EXTERNAL_TOOL_AGENT_ENABLED"


def external_tool_agent_enabled() -> bool:
    return os.environ.get(FEATURE_FLAG, "0").lower() in {"1", "true", "yes"}


def run_external_tool_agent(
    question: str,
    *,
    resolver: BrandResolver,
    external: ExternalApiClient,
    provider: ToolChoiceProvider | None = None,
) -> dict[str, Any]:
    registry = ExternalToolRegistry(resolver=resolver, external=external)
    selected_provider = provider or GenosToolChoiceProvider.from_env()
    result = AgentExecutor(provider=selected_provider).run(user_text=question, tools=registry.list_for_query(question))
    if result.fallback_code is not None:
        LOGGER.info("external tool agent fallback code=%s", result.fallback_code.value)
    return _agent_result_payload(question, result)


def _agent_result_payload(question: str, result: AgentResult) -> dict[str, Any]:
    return {
        "question": question,
        "resolution": None,
        "decomposition": [{"intent": "external_tool_agent", "status": result.status}],
        "router_diagnostics": {"mode": "tool_use_agent", "fallback_code": result.fallback_code.value if result.fallback_code else None},
        "tool_calls": list(result.tool_calls),
        "answer": result.answer,
        "markdown_response": {
            "markdown": result.answer,
            "fact_md": result.answer if result.status == "ok" else "",
            "data_md": "",
            "verification": {"status": "pass" if result.status == "ok" else "fail"},
        },
        "sources": list(result.sources),
        "agent_trace": [trace.model_dump() for trace in result.traces],
        "agent_loop_metrics": {
            "status": result.status,
            "tool_calls": len(result.tool_calls),
            "fallback_code": result.fallback_code.value if result.fallback_code else None,
        },
    }
