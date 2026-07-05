from __future__ import annotations

from jw_chat_agent_poc.agent_loop.factory import (
    build_agent_loop_dependencies,
    unsupported_brand_result,
)
from jw_chat_agent_poc.orchestrator.router_diagnostics import router_diagnostics
from jw_chat_agent_poc.router import BQRouter


def test_agent_loop_factory_preserves_external_mode_and_default_query_layer(monkeypatch) -> None:
    # Given: the cache-backed query layer is enabled exactly as ChatAgent currently does it.
    monkeypatch.setenv("CHAT_QUERY_LAYER_ENABLED", "1")
    monkeypatch.setenv("CHAT_METRICS_MODE", "cache")

    # When: shared agent-loop dependencies are created for live external mode.
    deps = build_agent_loop_dependencies(external_mode="live")

    # Then: the injected dependencies preserve the mode and cache query-layer contract.
    assert deps.external.mode == "live"
    assert deps.query_layer is not None


def test_agent_loop_factory_preserves_disabled_query_layer(monkeypatch) -> None:
    # Given: the query layer is disabled by environment configuration.
    monkeypatch.setenv("CHAT_QUERY_LAYER_ENABLED", "0")
    monkeypatch.setenv("CHAT_METRICS_MODE", "cache")

    # When: shared agent-loop dependencies are created.
    deps = build_agent_loop_dependencies(external_mode="fixture")

    # Then: the query layer remains absent, matching the old ChatAgent private helper.
    assert deps.query_layer is None


def test_unsupported_brand_result_matches_chat_agent_contract() -> None:
    # Given: a route from the existing router surface.
    router = BQRouter()
    routes = router.route("타이레놀 매출 알려줘", has_documents=False)

    # When: the shared unsupported-brand helper builds the response.
    result = unsupported_brand_result("타이레놀 매출 알려줘", routes, router_diagnostics(router))

    # Then: the response preserves ChatAgent's current unsupported contract.
    assert result["question"] == "타이레놀 매출 알려줘"
    assert result["resolution"] is None
    assert result["tool_calls"] == []
    assert result["sources"] == ["unsupported_brand"]
    assert result["router_diagnostics"] == router_diagnostics(router)
    assert "지원하지 않는 브랜드" in result["answer"]
    assert result["markdown_response"]["sources_md"].startswith("## 출처")
