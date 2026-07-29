from __future__ import annotations

from jw_chat_agent_poc.agent_loop.factory import (
    ambiguous_brand_result,
    build_agent_loop_dependencies,
    unsupported_brand_result,
    unsupported_hira_interface_result,
)
from jw_chat_agent_poc.orchestrator.markdown_formatting import source_description
from jw_chat_agent_poc.orchestrator.router_diagnostics import router_diagnostics
from jw_chat_agent_poc.router import BQRouter
from jw_chat_agent_poc.tools.external.cached_client import CachedExternalApiClient


def test_agent_loop_factory_preserves_external_mode_and_default_query_layer(monkeypatch) -> None:
    # Given: the cache-backed query layer is enabled exactly as ChatAgent currently does it.
    monkeypatch.setenv("CHAT_QUERY_LAYER_ENABLED", "1")
    monkeypatch.setenv("CHAT_METRICS_MODE", "cache")

    # When: shared agent-loop dependencies are created for live external mode.
    deps = build_agent_loop_dependencies(external_mode="live")

    # Then: the injected dependencies preserve the mode and use the serving-mart query layer.
    assert deps.external.mode == "live"
    assert deps.query_layer is not None
    assert not hasattr(deps.query_layer, "_cause_backend")


def test_agent_loop_factory_preserves_disabled_query_layer(monkeypatch) -> None:
    # Given: the query layer is disabled by environment configuration.
    monkeypatch.setenv("CHAT_QUERY_LAYER_ENABLED", "0")
    monkeypatch.setenv("CHAT_METRICS_MODE", "cache")

    # When: shared agent-loop dependencies are created.
    deps = build_agent_loop_dependencies(external_mode="fixture")

    # Then: the query layer remains absent, matching the old ChatAgent private helper.
    assert deps.query_layer is None


def test_live_agent_factories_share_external_result_cache(monkeypatch) -> None:
    monkeypatch.setenv("CHAT_EXTERNAL_RESULT_CACHE_TTL_SECONDS", "90")
    monkeypatch.setenv("CHAT_EXTERNAL_RESULT_CACHE_MAX_ENTRIES", "32")

    first = build_agent_loop_dependencies(external_mode="live")
    second = build_agent_loop_dependencies(external_mode="live")

    assert first.external is not second.external
    assert first.external._result_cache is second.external._result_cache


def test_fixture_agent_factories_do_not_share_external_result_cache() -> None:
    first = build_agent_loop_dependencies(external_mode="fixture")
    second = build_agent_loop_dependencies(external_mode="fixture")

    assert not isinstance(first.external, CachedExternalApiClient)
    assert not isinstance(second.external, CachedExternalApiClient)


def test_unsupported_brand_result_reports_absence_from_strategic_mart() -> None:
    # Given: a route from the existing router surface.
    router = BQRouter()
    routes = router.route("타이레놀 매출 알려줘", has_documents=False)

    # When: the shared unsupported-brand helper builds the response.
    result = unsupported_brand_result("타이레놀 매출 알려줘", routes, router_diagnostics(router))

    # Then: the response distinguishes source absence from resolver blocking.
    assert result["question"] == "타이레놀 매출 알려줘"
    assert result["resolution"] is None
    assert result["tool_calls"] == []
    assert result["sources"] == ["unsupported_brand"]
    assert result["router_diagnostics"] == router_diagnostics(router)
    assert "일치하는 브랜드가 확인되지 않습니다" in result["answer"]
    assert "지원하지 않는 브랜드" not in result["answer"]
    assert result["markdown_response"]["sources_md"].startswith("## 출처")
    assert "브랜드 식별 미확인" in result["markdown_response"]["sources_md"]
    assert "지원 범위 밖" not in result["markdown_response"]["sources_md"]
    assert source_description("unsupported_brand") == "요청 이름과 일치하는 브랜드 미확인"


def test_unsupported_hira_interface_result_uses_interface_specific_source_label() -> None:
    # Given: the exact unsupported HIRA query and its existing typed-result path.
    router = BQRouter()
    question = "없는브랜드ABC 환자수 알려줘"
    routes = router.route(question, has_documents=False)

    # When: the shared HIRA-interface helper builds the response.
    result = unsupported_hira_interface_result(question, routes, router_diagnostics(router))

    # Then: the trace stays typed while the public source explains the interface limitation.
    assert result["sources"] == ["unsupported_hira_interface"]
    assert "HIRA 상병코드·질환명 직접 조회 미지원" in result["markdown_response"]["sources_md"]
    assert "브랜드 식별 미확인" not in result["markdown_response"]["sources_md"]
    assert "상병코드 또는 질환명 직접 조회" in result["answer"]


def test_ambiguous_brand_result_keeps_brand_specific_source_label() -> None:
    # Given: a real brand ambiguity with multiple candidates.
    router = BQRouter()
    question = "카나브패밀리 실적 어때?"
    routes = router.route(question, has_documents=False)

    # When: the ambiguity helper builds its existing typed response.
    result = ambiguous_brand_result(
        question,
        routes,
        router_diagnostics(router),
        ("카나브", "카나브젯", "카나브플러스"),
    )

    # Then: the genuine brand path retains its brand-candidate label.
    assert result["sources"] == ["ambiguous_brand"]
    assert "브랜드 식별 후보" in result["markdown_response"]["sources_md"]
    assert "HIRA 상병코드·질환명 직접 조회 미지원" not in result["markdown_response"]["sources_md"]
