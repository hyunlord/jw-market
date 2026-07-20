from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import requests

from jw_chat_agent_poc.resolver import BrandResolver
from jw_chat_agent_poc.tool_use.integration import run_external_tool_agent
from jw_chat_agent_poc.tool_use.provider import ToolChoice
from jw_chat_agent_poc.tools.external import ExternalApiClient


@dataclass(slots=True)
class _ChoiceSequence:
    choices: Sequence[ToolChoice]
    calls: int = field(default=0, init=False)

    def choose(self, *, user_text: str, messages: list[dict], tools: list[dict]) -> ToolChoice:
        del user_text, messages, tools
        choice = self.choices[self.calls]
        self.calls += 1
        return choice


class _TimeoutProvider:
    def choose(self, *, user_text: str, messages: list[dict], tools: list[dict]) -> ToolChoice:
        del user_text, messages, tools
        raise requests.Timeout("shadow planner timeout")


def _no_tool_provider(message: str = "no matching tool") -> _ChoiceSequence:
    return _ChoiceSequence((ToolChoice(None, {}, message, call_id=None),))


def _without_v4_diagnostics(payload: dict[str, Any]) -> dict[str, Any]:
    copied = dict(payload)
    diagnostics = dict(copied["router_diagnostics"])
    diagnostics.pop("routing_v4", None)
    copied["router_diagnostics"] = diagnostics
    return copied


def test_missing_invalid_and_explicit_off_modes_are_byte_equivalent(monkeypatch) -> None:
    question = "상병코드 D693의 최근 5개년 환자수 추이를 분석해줘"
    payloads: list[dict[str, Any]] = []

    for raw_mode in (None, "invalid-mode", "OFF"):
        if raw_mode is None:
            monkeypatch.delenv("CHAT_TOOL_ROUTING_MODE", raising=False)
        else:
            monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", raw_mode)
        payloads.append(
            run_external_tool_agent(
                question,
                resolver=BrandResolver(),
                external=ExternalApiClient(mode="fixture"),
                provider=_no_tool_provider(),
                routing_provider=_TimeoutProvider(),
            )
        )

    assert payloads[0] == payloads[1] == payloads[2]
    assert "routing_v4" not in payloads[0]["router_diagnostics"]


def test_shadow_records_prs_without_executing_or_changing_legacy_response(monkeypatch) -> None:
    question = "상병코드 D693의 최근 5개년 환자수 추이를 분석해줘"
    off_external = ExternalApiClient(mode="fixture")
    shadow_external = ExternalApiClient(mode="fixture")
    executions = 0
    original = shadow_external.hira_disease_hospitalization_outpatient_stats

    def counted_call(*, sick_cd: str, year: str = "2024"):
        nonlocal executions
        executions += 1
        return original(sick_cd=sick_cd, year=year)

    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "OFF")
    off = run_external_tool_agent(
        question,
        resolver=BrandResolver(),
        external=off_external,
        provider=_no_tool_provider(),
    )
    monkeypatch.setattr(shadow_external, "hira_disease_hospitalization_outpatient_stats", counted_call)
    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "SHADOW")
    shadow = run_external_tool_agent(
        question,
        resolver=BrandResolver(),
        external=shadow_external,
        provider=_no_tool_provider(),
        routing_provider=_no_tool_provider(),
    )

    assert executions == 0
    assert _without_v4_diagnostics(shadow) == off
    proposal = shadow["router_diagnostics"]["routing_v4"]["proposed_routing_signature"]
    assert proposal["routing_mode"] == "SHADOW"
    assert len(proposal["proposed_calls"]) == 5
    assert {call["tool_name"] for call in proposal["proposed_calls"]} == {
        "hira_disease_hospitalization_outpatient_stats"
    }


def test_shadow_planner_timeout_is_isolated_from_legacy_response(monkeypatch) -> None:
    question = "당뇨병성 황반부종(DME) 관련 임상시험을 찾아줘"
    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "OFF")
    off = run_external_tool_agent(
        question,
        resolver=BrandResolver(),
        external=ExternalApiClient(mode="fixture"),
        provider=_no_tool_provider(),
    )
    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "SHADOW")
    shadow = run_external_tool_agent(
        question,
        resolver=BrandResolver(),
        external=ExternalApiClient(mode="fixture"),
        provider=_no_tool_provider(),
        routing_provider=_TimeoutProvider(),
    )

    assert _without_v4_diagnostics(shadow) == off
    diagnostics = shadow["router_diagnostics"]["routing_v4"]
    assert diagnostics["shadow_status"] == "error"
    assert diagnostics["shadow_error"] == "Timeout"


def test_enforce_executes_the_prs_and_emits_ordered_ccs(monkeypatch) -> None:
    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "ENFORCE")

    payload = run_external_tool_agent(
        "아일리아의 허가 품목명과 업체명을 공식 허가정보 기준으로 알려줘",
        resolver=BrandResolver(),
        external=ExternalApiClient(mode="fixture"),
        provider=_no_tool_provider(),
        routing_provider=_TimeoutProvider(),
    )

    assert [call["tool"] for call in payload["tool_calls"]] == ["mfds_permission_search"]
    diagnostics = payload["router_diagnostics"]["routing_v4"]
    ccs = diagnostics["executed_call_signature"]
    assert ccs["routing_mode"] == "ENFORCE"
    assert ccs["routing_decision"]["tool_selection_source"] == "DETERMINISTIC_SINGLETON"
    assert ccs["executed_calls"] == [
        {
            "call_ordinal": 1,
            "parent_ordinal": None,
            "tool_name": "mfds_permission_search",
            "normalized_args": {"brand": "아일리아"},
            "result_status": payload["tool_calls"][0]["status"],
        }
    ]


def test_enforce_typed_stop_does_not_execute_web_fallback(monkeypatch) -> None:
    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "ENFORCE")

    payload = run_external_tool_agent(
        "아일리아의 급여기준에 대해서 적응증 별로 설명해줘",
        resolver=BrandResolver(),
        external=ExternalApiClient(mode="fixture"),
        provider=_no_tool_provider(),
        routing_provider=_TimeoutProvider(),
    )

    assert payload["tool_calls"] == []
    assert "web" not in payload["answer"].casefold()
    ccs = payload["router_diagnostics"]["routing_v4"]["executed_call_signature"]
    assert ccs["routing_decision"]["capability_status"] == "NOT_IMPLEMENTED"
    assert ccs["routing_decision"]["route_outcome"] == "TYPED_STOP"
    assert ccs["reason_code"] == "CAPABILITY_NOT_IMPLEMENTED"
    assert ccs["executed_calls"] == []
