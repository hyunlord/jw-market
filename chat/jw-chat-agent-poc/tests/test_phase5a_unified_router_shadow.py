from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from jw_chat_agent_poc.contracts.routing import RouteMode
from jw_chat_agent_poc.orchestrator.shadow_gate_runtime import shadow_request_scope
from jw_chat_agent_poc.orchestrator.unified_router import (
    UNIFIED_ROUTER_SHADOW_ENV,
    AppScopeSignals,
    MarketShortcutSignals,
    PlannerSignals,
    SecurityVerdict,
    UnifiedRouteInput,
    compare_with_legacy,
    route,
)
from jw_chat_agent_poc.orchestrator import unified_router_shadow
from jw_chat_agent_poc.service import app as service_app
from jw_chat_agent_poc.service.context_scope import ContextScope
from jw_chat_agent_poc.service.routing_boundary_contract import MarketRouteKind
from jw_chat_agent_poc.tool_use.routing_v4_rules import classify_question_without_observation

from test_service import FakeAgent, _market_scope_resolver


FIXTURES = Path(__file__).parent / "characterization" / "fixtures"


def _market_input(question: str) -> UnifiedRouteInput:
    return UnifiedRouteInput(
        question=question,
        security_verdict=SecurityVerdict.ALLOW,
        app_scope=AppScopeSignals(
            file_question=question,
            effective_question=question,
            has_file=False,
            is_fresh_upload=False,
            has_market_intent=True,
            has_market_anchor=True,
            file_schema_columns=(),
            needs_brand_clarification=False,
            needs_market_clarification=False,
        ),
        market_shortcut=MarketShortcutSignals(
            has_documents=False,
            use_direct_agent_loop=True,
            market_scope_resolver=_market_scope_resolver(),
        ),
    )


def test_divergent_case_is_represented_as_two_canonical_fields() -> None:
    decision = route(_market_input("리바로 매출 알려줘"))

    assert decision.context_scope == ContextScope.MARKET.value
    assert decision.handler == "agent_loop"
    assert decision.execution_mode is RouteMode.AGENTIC
    assert decision.tool_plan_owner == "agent_loop_planner"
    assert decision.decided_layers == ("app_scope", "market_shortcut")
    assert decision.context_handler == "context_scope_dispatch"


def test_planner_layer_does_not_overwrite_market_context_or_handler() -> None:
    route_input = _market_input("리바로 매출 알려줘")
    decision = route(
        UnifiedRouteInput(
            question=route_input.question,
            security_verdict=route_input.security_verdict,
            app_scope=route_input.app_scope,
            market_shortcut=route_input.market_shortcut,
            planner=PlannerSignals(
                selected_handler="deterministic_bq_plan",
                deterministic_plan=True,
                planner_kind="BQ:C1",
            ),
        )
    )

    assert decision.context_scope == ContextScope.MARKET.value
    assert decision.handler == "agent_loop"
    assert decision.execution_mode is RouteMode.AGENTIC
    assert decision.tool_plan_owner == "agent_loop_planner"
    assert decision.tool_plan_handler == "deterministic_bq_plan"
    assert decision.tool_plan_mode is RouteMode.DETERMINISTIC


def test_security_block_precedes_domain_routing() -> None:
    route_input = _market_input("리바로 매출 알려줘")

    decision = route(
        UnifiedRouteInput(
            question=route_input.question,
            security_verdict=SecurityVerdict.BLOCK,
            app_scope=route_input.app_scope,
            market_shortcut=route_input.market_shortcut,
        )
    )

    assert decision.handler == "security_block"
    assert decision.execution_mode is RouteMode.DETERMINISTIC
    assert decision.reason_codes == ("security:blocked",)


def test_characterization_corpus_accounts_for_all_512_route_point_slots() -> None:
    corpus = json.loads((FIXTURES / "corpus.v1.json").read_text(encoding="utf-8"))
    comparisons = []

    for case in corpus["cases"]:
        question = case["question"]
        legacy = classify_question_without_observation(question)
        unified = route(
            UnifiedRouteInput(
                question=question,
                security_verdict=SecurityVerdict.ALLOW,
            )
        )
        comparisons.append(
            compare_with_legacy(
                unified,
                decided_by="routing_v4_rules",
                legacy_domain=legacy.source_domain,
                legacy_handler=legacy.requested_capability,
                legacy_mode=(
                    RouteMode.AGENTIC
                    if legacy.domain_decision_source.value == "LLM"
                    else RouteMode.DETERMINISTIC
                ),
            )
        )

    raw_input_unavailable = {
        "app_scope": len(corpus["cases"]),
        "market_shortcut": len(corpus["cases"]),
        "agent_loop_planner": len(corpus["cases"]),
    }
    totals = {
        "match": sum(comparison.matches for comparison in comparisons),
        "mismatch": sum(not comparison.matches for comparison in comparisons),
        "raw_input_unavailable": sum(raw_input_unavailable.values()),
    }

    assert len(corpus["cases"]) == 128
    assert totals == {"match": 128, "mismatch": 0, "raw_input_unavailable": 384}
    assert sum(totals.values()) == 128 * 4


def test_shadow_result_is_not_consumed_by_existing_execution(monkeypatch) -> None:
    baseline = service_app._answer_question(
        service_app.SessionStore(),
        _market_scope_resolver(),
        lambda **_kwargs: FakeAgent(external_mode="fixture"),
        "리바로 매출 알려줘",
        "fixture",
        "phase5a-baseline",
    )

    monkeypatch.setattr(
        unified_router_shadow,
        "route",
        lambda _route_input: (_ for _ in ()).throw(RuntimeError("must stay shadow")),
    )
    actual = service_app._answer_question(
        service_app.SessionStore(),
        _market_scope_resolver(),
        lambda **_kwargs: FakeAgent(external_mode="fixture"),
        "리바로 매출 알려줘",
        "fixture",
        "phase5a-shadow-failure",
    )

    assert actual["result"]["answer"].encode() == baseline["result"]["answer"].encode()
    assert actual["result"].get("charts") == baseline["result"].get("charts")


def test_four_shadow_comparisons_use_existing_request_id(monkeypatch) -> None:
    payloads: list[dict[str, object]] = []
    monkeypatch.setattr(unified_router_shadow, "_write_payload", payloads.append)

    @shadow_request_scope
    def emit_all_four_points() -> None:
        unified_router_shadow.observe_app_scope_route(
            question="리바로 매출 알려줘",
            file_question="리바로 매출 알려줘",
            effective_question="리바로 매출 알려줘",
            has_file=False,
            is_fresh_upload=False,
            has_market_intent=True,
            has_market_anchor=True,
            file_schema_columns=(),
            needs_brand_clarification=False,
            needs_market_clarification=False,
            legacy_domain=ContextScope.MARKET.value,
            legacy_handler="context_scope_dispatch",
            legacy_mode=RouteMode.DETERMINISTIC,
            deep_research=False,
        )
        unified_router_shadow.observe_market_shortcut_route(
            question="리바로 매출 알려줘",
            has_documents=False,
            use_direct_agent_loop=True,
            market_scope_resolver=_market_scope_resolver(),
            legacy_domain="market",
            legacy_handler="agent_loop",
            legacy_mode=RouteMode.AGENTIC,
        )
        unified_router_shadow.observe_routing_v4_route(
            question="리바로 매출 알려줘",
            legacy_domain="unresolved",
            legacy_handler="UNCLASSIFIED_EXTERNAL_REQUEST",
            legacy_mode=RouteMode.DETERMINISTIC,
        )
        unified_router_shadow.observe_agent_planner_route(
            question="리바로 매출 알려줘",
            selected_handler="deterministic_bq_plan",
            deterministic_plan=True,
            planner_kind="BQ:C1",
            legacy_mode=RouteMode.DETERMINISTIC,
        )

    emit_all_four_points()

    assert len(payloads) == 4
    assert len({payload["request_id"] for payload in payloads}) == 1
    assert {payload["legacy_decided_by"] for payload in payloads} == {
        "app_scope",
        "market_shortcut",
        "routing_v4_rules",
        "agent_loop_planner",
    }
    assert all(payload["answer_action"] == "unchanged" for payload in payloads)


def test_flag_off_does_not_call_unified_router(monkeypatch) -> None:
    monkeypatch.setenv(UNIFIED_ROUTER_SHADOW_ENV, "0")
    monkeypatch.setattr(
        unified_router_shadow,
        "route",
        lambda _route_input: (_ for _ in ()).throw(AssertionError("route() must not run")),
    )

    result = service_app._answer_question(
        service_app.SessionStore(),
        _market_scope_resolver(),
        lambda **_kwargs: FakeAgent(external_mode="fixture"),
        "리바로 매출 알려줘",
        "fixture",
        "phase5a-flag-off",
    )

    assert result["result"]["answer"]


def test_flag_off_imports_routing_modules_without_unified_router() -> None:
    project_root = Path(__file__).resolve().parents[1]
    blocked_modules = (
        "jw_chat_agent_poc.orchestrator.unified_router",
        "jw_chat_agent_poc.orchestrator.unified_router_shadow",
    )
    script = f"""
import sys

class BlockUnifiedRouter:
    def find_spec(self, fullname, path, target=None):
        if fullname in {blocked_modules!r}:
            raise RuntimeError("unified router import attempted")
        return None

sys.meta_path.insert(0, BlockUnifiedRouter())
from jw_chat_agent_poc.service import app
from jw_chat_agent_poc.tool_use import routing_v4_rules
from jw_chat_agent_poc.agent_loop import loop
assert all(module not in sys.modules for module in {blocked_modules!r})
assert not app.unified_router_shadow_enabled()
assert routing_v4_rules.classify_question("리바로 매출").source_domain == "unresolved"
assert loop.unified_router_shadow_enabled() is False
"""
    env = dict(os.environ)
    env[UNIFIED_ROUTER_SHADOW_ENV] = "0"

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_market_shortcut_comparison_maps_existing_fields() -> None:
    unified = route(_market_input("리바로 매출 알려줘"))
    comparison = compare_with_legacy(
        unified,
        decided_by="market_shortcut",
        legacy_domain="market",
        legacy_handler="agent_loop",
        legacy_mode=RouteMode.AGENTIC,
    )

    assert comparison.matches is True
    assert comparison.unavailable_fields == ()
    assert unified.market_route_kind == MarketRouteKind.AGENT_LOOP.value
