from __future__ import annotations

from jw_chat_agent_poc.contracts.routing import (
    RejectedRoute,
    RouteDecision,
    RouteMode,
)
from jw_chat_agent_poc.orchestrator import route_decision_shadow
from jw_chat_agent_poc.orchestrator.shadow_gate_runtime import shadow_request_scope
from jw_chat_agent_poc.agent_loop.loop import ToolUseAgent
from jw_chat_agent_poc.agent_loop.models import AgentDecision
from jw_chat_agent_poc.resolver import BrandResolver
from jw_chat_agent_poc.service import app as service_app
from jw_chat_agent_poc.tool_use import routing_v4_rules

from test_service import FakeAgent, _market_scope_resolver
from test_stage2_agent_loop import ScriptedPlanner, _metrics_tool


def _decision(decided_by: str) -> RouteDecision:
    return RouteDecision(
        domain="market",
        handler="fixture_handler",
        mode=RouteMode.DETERMINISTIC,
        decided_by=decided_by,
        reason_codes=("fixture:selected",),
        rejected_alternatives=(
            RejectedRoute(
                domain="external",
                handler="external_agent",
                reason_codes=("fixture:not_selected",),
            ),
        ),
    )


def test_route_decisions_share_request_id_and_distinguish_four_points(
    monkeypatch,
) -> None:
    payloads: list[dict[str, object]] = []
    monkeypatch.setattr(
        route_decision_shadow,
        "_write_route_decision_payload",
        payloads.append,
    )

    @shadow_request_scope
    def _emit_request() -> None:
        for decided_by in (
            "app_scope",
            "market_shortcut",
            "routing_v4_rules",
            "agent_loop_planner",
        ):
            route_decision_shadow.emit_route_decision(
                _decision(decided_by),
                question="리바로 매출 추이",
            )

    _emit_request()

    assert len(payloads) == 4
    assert len({payload["request_id"] for payload in payloads}) == 1
    assert payloads[0]["request_id"]
    assert len({payload["observation_id"] for payload in payloads}) == 4
    assert {payload["route_decision"]["decided_by"] for payload in payloads} == {
        "app_scope",
        "market_shortcut",
        "routing_v4_rules",
        "agent_loop_planner",
    }
    assert all(
        payload["route_decision"]["rejected_alternatives"]
        for payload in payloads
    )


def test_route_decision_observation_is_fail_open(monkeypatch) -> None:
    baseline = {"answer": "동일 답변", "chart": {"dataset": [1, 2]}}

    def _fail(_payload: dict[str, object]) -> None:
        raise RuntimeError("synthetic telemetry sink failure")

    monkeypatch.setattr(
        route_decision_shadow,
        "_write_route_decision_payload",
        _fail,
    )

    route_decision_shadow.emit_route_decision(
        _decision("app_scope"),
        question="리바로 매출 추이",
    )

    assert baseline == {"answer": "동일 답변", "chart": {"dataset": [1, 2]}}


def test_actual_app_scope_and_market_shortcut_emit_distinct_points(monkeypatch) -> None:
    observed: list[str] = []
    monkeypatch.setattr(
        service_app,
        "observe_route_decision",
        lambda **fields: observed.append(fields["decided_by"]),
    )

    service_app._answer_question(
        service_app.SessionStore(),
        _market_scope_resolver(),
        lambda **_kwargs: FakeAgent(external_mode="fixture"),
        "리바로 매출 알려줘",
        "fixture",
        "phase3-routing-observation",
    )

    assert "app_scope" in observed
    assert "market_shortcut" in observed


def test_sink_failure_keeps_actual_answer_and_chart_bytes(monkeypatch) -> None:
    baseline = service_app._answer_question(
        service_app.SessionStore(),
        _market_scope_resolver(),
        lambda **_kwargs: FakeAgent(external_mode="fixture"),
        "리바로 매출 알려줘",
        "fixture",
        "phase3-fail-open-baseline",
    )

    def _fail(_payload: dict[str, object]) -> None:
        raise RuntimeError("synthetic telemetry sink failure")

    monkeypatch.setattr(
        route_decision_shadow,
        "_write_route_decision_payload",
        _fail,
    )
    actual = service_app._answer_question(
        service_app.SessionStore(),
        _market_scope_resolver(),
        lambda **_kwargs: FakeAgent(external_mode="fixture"),
        "리바로 매출 알려줘",
        "fixture",
        "phase3-fail-open-actual",
    )

    assert actual["result"]["answer"].encode() == baseline["result"]["answer"].encode()
    assert actual["result"].get("charts") == baseline["result"].get("charts")


def test_contract_creation_failure_is_fail_open(monkeypatch) -> None:
    def _fail_contract(**_fields):
        raise RuntimeError("synthetic contract creation failure")

    monkeypatch.setattr(route_decision_shadow, "RouteDecision", _fail_contract)

    assert (
        route_decision_shadow.observe_route_decision(
            question="리바로 매출 추이",
            domain="market",
            handler="fixture",
            mode=RouteMode.DETERMINISTIC,
            decided_by="app_scope",
            reason_codes=("fixture",),
        )
        is None
    )


def test_actual_routing_v4_rule_emits_decision(monkeypatch) -> None:
    observed: list[dict[str, object]] = []
    monkeypatch.setattr(
        routing_v4_rules,
        "observe_route_decision",
        lambda **fields: observed.append(fields),
    )

    classification = routing_v4_rules.classify_question("아일리아 급여기준 알려줘")

    assert classification.requested_capability == "HIRA_REIMBURSEMENT_CRITERIA"
    assert observed[0]["decided_by"] == "routing_v4_rules"
    assert observed[0]["rejected_alternatives"]


def test_actual_agent_loop_planner_emits_decision(monkeypatch) -> None:
    observed: list[dict[str, object]] = []
    from jw_chat_agent_poc.agent_loop import loop

    monkeypatch.setattr(
        loop,
        "observe_route_decision",
        lambda **fields: observed.append(fields),
    )
    agent = ToolUseAgent(
        metrics=_metrics_tool(),
        resolver=BrandResolver(),
        planner=ScriptedPlanner((AgentDecision(final_answer="도구 결과로 답변"),)),
    )

    agent.answer("리바로 요약해줘")

    assert observed
    assert {item["decided_by"] for item in observed} == {"agent_loop_planner"}
    assert all(item["rejected_alternatives"] for item in observed)
