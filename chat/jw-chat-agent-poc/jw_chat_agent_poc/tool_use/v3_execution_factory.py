from __future__ import annotations

from jw_chat_agent_poc.tool_use.v3_execution import V3ShadowToolExecutor
from jw_chat_agent_poc.tool_use.v3_execution_tools import (
    external_executable_tools,
    internal_executable_tools,
)


def build_default_shadow_executor(question: str) -> V3ShadowToolExecutor:
    """Construct read-only tools only after the execution flag is enabled."""

    from jw_chat_agent_poc.agent_loop.factory import build_agent_loop_dependencies
    from jw_chat_agent_poc.tool_use.internal_adapters import (
        InternalToolAdapterRegistry,
    )
    from jw_chat_agent_poc.tool_use.registry import ExternalToolRegistry

    dependencies = build_agent_loop_dependencies(external_mode="live")
    if dependencies.query_layer is None:
        raise RuntimeError("V3 shadow execution requires the read-only query layer")
    external_registry = ExternalToolRegistry(
        resolver=dependencies.resolver,
        external=dependencies.external,
    )
    specs = {
        spec.name: spec
        for spec in external_registry.list_for_query(question)
    }
    for spec in external_registry.list_for_query(""):
        specs.setdefault(spec.name, spec)
    internal_registry = InternalToolAdapterRegistry(
        market_layer=dependencies.query_layer,
    )
    return V3ShadowToolExecutor(
        tools=(
            *external_executable_tools(tuple(specs.values())),
            *internal_executable_tools(internal_registry),
        )
    )
