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
    from jw_chat_agent_poc.tool_use.market_scope_execution import (
        MarketScopeCatalogBackend,
        ScopeResolver,
    )
    from jw_chat_agent_poc.tool_use.registry import ExternalToolRegistry
    from jw_chat_agent_poc.service.general_view_routing import GeneralViewService
    from jw_chat_agent_poc.tools.general_view_backend import GeneralViewBackend
    from jw_chat_agent_poc.tools.general_view_mart import (
        GeneralViewMartBackend,
        MariaDbGeneralMartReader,
    )
    from jw_chat_agent_poc.tools.general_view_membership import (
        MariaDbGeneralMembershipReader,
        TtlGeneralMembershipCache,
    )

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
    general_membership = TtlGeneralMembershipCache(
        MariaDbGeneralMembershipReader(),
        ttl_seconds=300,
    )
    general_backend = GeneralViewMartBackend(
        MariaDbGeneralMartReader(),
        GeneralViewBackend(),
        allow_fallback=False,
    )
    scope_resolver = ScopeResolver(
        strategic_memberships=dependencies.query_layer.brand_memberships,
        general_membership=general_membership,
        route_hint=GeneralViewService(
            general_backend,
            dependencies.resolver,
            enabled=True,
            general_membership=general_membership,
        ).route(question).value,
    )
    internal_registry = InternalToolAdapterRegistry(
        market_layer=MarketScopeCatalogBackend(
            dependencies.query_layer,
            scope_resolver,
            general_backend,
        ),
    )
    return V3ShadowToolExecutor(
        tools=(
            *external_executable_tools(tuple(specs.values())),
            *internal_executable_tools(internal_registry),
        )
    )
