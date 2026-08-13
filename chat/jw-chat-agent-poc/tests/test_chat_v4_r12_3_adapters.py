from __future__ import annotations

from types import SimpleNamespace

from jw_chat_agent_poc.tools.external.client import ExternalCall


def _patch_dependencies(monkeypatch, resolver, external) -> None:
    from jw_chat_agent_poc.agent_loop import factory
    from jw_chat_agent_poc.service import general_view_routing

    monkeypatch.setattr(
        factory,
        "build_chat_agent_dependencies",
        lambda **_kwargs: SimpleNamespace(
            external=external,
            resolver=resolver,
            query_layer=None,
        ),
    )
    monkeypatch.setattr(
        general_view_routing.GeneralViewService,
        "from_env",
        lambda _resolver: SimpleNamespace(),
    )


def test_f_ingredient_only_nedrug_query_skips_meaningless_item_name_call(
    monkeypatch,
) -> None:
    from jw_chat_agent_poc.service.v4 import adapters as v4_adapters

    calls: list[str] = []

    class Resolver:
        def resolve(self, _query: str, *, allow_default: bool):
            assert allow_default is False
            raise LookupError

    class External:
        timeout_s = 12

        def mfds_permission_search(self, item_name: str) -> ExternalCall:
            calls.append(item_name)
            return ExternalCall(
                tool="nedrug_permission_search",
                source="nedrug_mcp",
                status="no_data",
                summary_text="no data",
                render_data={},
            )

    _patch_dependencies(monkeypatch, Resolver(), External())

    result = v4_adapters.build_source_adapters()["nedrug"](
        "pitavastatin 성분 품목을 알려줘"
    )

    assert calls == []
    assert result.status == "scope_limit"
    assert "성분명으로는 품목 검색이 지원되지 않아" in str(result.notice)


def test_f_known_brand_nedrug_query_keeps_existing_call_path(monkeypatch) -> None:
    from jw_chat_agent_poc.service.v4 import adapters as v4_adapters

    calls: list[str] = []

    class Resolver:
        def resolve(self, _query: str, *, allow_default: bool):
            assert allow_default is False
            return SimpleNamespace(canonical_brand="리바로", market_ids=())

    class External:
        timeout_s = 12

        def mfds_permission_search(self, item_name: str) -> ExternalCall:
            calls.append(item_name)
            return ExternalCall(
                tool="nedrug_permission_search",
                source="nedrug_mcp",
                status="live",
                summary_text="one item",
                render_data={"items": [{"item_name": "리바로정"}]},
            )

    _patch_dependencies(monkeypatch, Resolver(), External())

    result = v4_adapters.build_source_adapters()["nedrug"]("리바로 허가사항")

    assert calls == ["리바로"]
    assert result.status == "ok"


def test_f_unresolved_product_with_ingredient_term_keeps_search_path(monkeypatch) -> None:
    from jw_chat_agent_poc.service.v4 import adapters as v4_adapters

    calls: list[str] = []

    class Resolver:
        def resolve(self, _query: str, *, allow_default: bool):
            assert allow_default is False
            raise LookupError

    class External:
        timeout_s = 12

        def mfds_permission_search(self, item_name: str) -> ExternalCall:
            calls.append(item_name)
            return ExternalCall(
                tool="nedrug_permission_search",
                source="nedrug_mcp",
                status="no_data",
                summary_text="no data",
                render_data={},
            )

    _patch_dependencies(monkeypatch, Resolver(), External())

    result = v4_adapters.build_source_adapters()["nedrug"](
        "미해결브랜드정 피타바스타틴 성분 허가사항"
    )

    assert calls == ["미해결브랜드정 피타바스타틴 성분 허가사항"]
    assert result.status == "empty"
