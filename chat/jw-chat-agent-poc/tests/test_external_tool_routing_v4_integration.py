from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import requests

from jw_chat_agent_poc.resolver import BrandResolver
from jw_chat_agent_poc.tool_use.integration import run_external_tool_agent
from jw_chat_agent_poc.tool_use.provider import ToolChoice
from jw_chat_agent_poc.tool_use.routing_v4_rules import QuestionClassification
from jw_chat_agent_poc.tool_use.routing_v4_types import (
    DomainDecisionSource,
    ProposedCall,
)
from jw_chat_agent_poc.tools.external import ExternalApiClient
from jw_chat_agent_poc.tools.external import ExternalCall


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


def test_force_flag_does_not_disable_shadow_routing_provider(monkeypatch) -> None:
    provider = _ChoiceSequence(
        (
            ToolChoice(
                "clinicaltrials_v2_search",
                {"query": "diabetic macular edema", "query_type": "condition"},
                "bounded candidate",
                call_id="proposal-1",
            ),
        )
    )
    monkeypatch.setenv("CHAT_EXTERNAL_TOOL_FORCE_CONTRACT_CALLS", "true")
    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "SHADOW")

    payload = run_external_tool_agent(
        "당뇨병성 황반부종(DME) 관련 임상시험을 찾아줘",
        resolver=BrandResolver(),
        external=ExternalApiClient(mode="fixture"),
        provider=_no_tool_provider(),
        routing_provider=provider,
    )

    assert provider.calls == 1
    proposal = payload["router_diagnostics"]["routing_v4"]["proposed_routing_signature"]
    assert proposal["routing_decision"]["tool_selection_source"] == "LLM"
    assert proposal["proposed_calls"][0]["tool_name"] == "clinicaltrials_v2_search"


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


def test_a13_preserves_all_exact_family_rows_and_binds_each_claim(monkeypatch) -> None:
    external = ExternalApiClient(mode="fixture")
    rows = [
        {
            "ITEM_SEQ": f"item-{index}",
            "ITEM_NAME": f"아일리아주사{index}",
            "ENTP_NAME": f"제조사{index}",
            "ITEM_PERMIT_DATE": f"20240{index}01",
        }
        for index in range(1, 5)
    ]

    def permission_search(brand: str) -> ExternalCall:
        assert brand == "아일리아"
        return ExternalCall(
            tool="mfds_permission_search",
            source="external_api",
            status="ok",
            summary_text="exact family 4 rows",
            render_data={"items": rows, "request": {"brand": brand}},
        )

    monkeypatch.setattr(external, "mfds_permission_search", permission_search)
    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "ENFORCE")
    payload = run_external_tool_agent(
        "아일리아의 허가 품목명과 업체명을 공식 허가정보 기준으로 알려줘",
        resolver=BrandResolver(),
        external=external,
        provider=_no_tool_provider(),
    )

    for index in range(1, 5):
        assert f"아일리아주사{index}" in payload["answer"]
        assert f"제조사{index}" in payload["answer"]
    evidence = payload["tool_calls"][0]["render_data"]["evidence"]
    assert [fact["fact_id"] for fact in evidence] == [
        "mfds_permission_search:1",
        "mfds_permission_search:2",
        "mfds_permission_search:3",
        "mfds_permission_search:4",
    ]
    diagnostics = payload["router_diagnostics"]["routing_v4"]
    assert diagnostics["claim_evidence_binding_status"] == "pass"
    assert diagnostics["claim_evidence_bindings"] == [
        {
            "claim_ordinal": index,
            "tool_name": "mfds_permission_search",
            "evidence_ids": [f"mfds_permission_search:{index}"],
        }
        for index in range(1, 5)
    ]


def test_a03_exact_code_zero_rows_is_no_record_found_not_parent_substitution(monkeypatch) -> None:
    external = ExternalApiClient(mode="fixture")
    observed_codes: list[str] = []

    def no_rows(sick_cd: str, year: str = "2024") -> ExternalCall:
        observed_codes.append(sick_cd)
        return ExternalCall(
            tool="hira_disease_hospitalization_outpatient_stats",
            source="external_api",
            status="ok",
            summary_text="zero exact rows",
            render_data={"items": [], "request": {"sick_cd": sick_cd, "year": year}},
        )

    monkeypatch.setattr(external, "hira_disease_hospitalization_outpatient_stats", no_rows)
    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "ENFORCE")
    payload = run_external_tool_agent(
        "질병코드 H360 환자수 통계 알려줘",
        resolver=BrandResolver(),
        external=external,
        provider=_no_tool_provider(),
    )

    assert observed_codes == ["H36.0"]
    ccs = payload["router_diagnostics"]["routing_v4"]["executed_call_signature"]
    assert ccs["reason_code"] == "NO_RECORD_FOUND"
    assert ccs["runtime_status"] == "typed_stop"
    assert "web_search" not in [call["tool"] for call in payload["tool_calls"]]


def test_a01_partial_periods_preserve_official_rows_without_trend_claim(monkeypatch) -> None:
    external = ExternalApiClient(mode="fixture")

    def period_rows(sick_cd: str, year: str = "2024") -> ExternalCall:
        assert sick_cd == "D69.3"
        available = year != "2022"
        return ExternalCall(
            tool="hira_disease_hospitalization_outpatient_stats",
            source="external_api",
            status="ok" if available else "no_data",
            summary_text=f"{year} {'one row' if available else 'no rows'}",
            render_data={
                "items": ([{"year": year, "value": str(100 + int(year))}] if available else []),
                "request": {"sick_cd": sick_cd, "year": year},
            },
        )

    monkeypatch.setattr(external, "hira_disease_hospitalization_outpatient_stats", period_rows)
    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "ENFORCE")
    payload = run_external_tool_agent(
        "상병코드 D693의 최근 5개년 환자수 추이를 분석해줘",
        resolver=BrandResolver(),
        external=external,
        provider=_no_tool_provider(),
    )

    ccs = payload["router_diagnostics"]["routing_v4"]["executed_call_signature"]
    assert ccs["reason_code"] == "PARTIAL_RESULT"
    assert ccs["runtime_status"] == "partial"
    assert len(ccs["executed_calls"]) == 5
    assert "web_search" not in [call["tool"] for call in payload["tool_calls"]]
    assert not any(token in payload["answer"] for token in ("연속 상승", "연속 하락", "반등", "정점"))


def test_d06_authoritative_timeout_stops_without_web_fallback(monkeypatch) -> None:
    external = ExternalApiClient(mode="fixture")

    def timeout(*, sick_cd: str, year: str = "2024") -> ExternalCall:
        del sick_cd, year
        raise requests.Timeout("authoritative timeout")

    monkeypatch.setattr(external, "hira_disease_hospitalization_outpatient_stats", timeout)
    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "ENFORCE")
    payload = run_external_tool_agent(
        "상병코드 D693의 최근 5개년 환자수 추이를 분석해줘",
        resolver=BrandResolver(),
        external=external,
        provider=_no_tool_provider(),
    )

    ccs = payload["router_diagnostics"]["routing_v4"]["executed_call_signature"]
    assert ccs["reason_code"] == "UPSTREAM_UNAVAILABLE"
    assert ccs["runtime_status"] == "typed_stop"
    assert "web_search" not in [call["tool"] for call in payload["tool_calls"]]


def test_d08_explicitly_truncated_result_fails_closed(monkeypatch) -> None:
    external = ExternalApiClient(mode="fixture")

    def truncated(brand: str) -> ExternalCall:
        return ExternalCall(
            tool="mfds_permission_search",
            source="external_api",
            status="ok",
            summary_text="upstream result truncated",
            render_data={
                "truncated": True,
                "items": [
                    {
                        "ITEM_SEQ": "201306324",
                        "ITEM_NAME": f"{brand}주사",
                        "ENTP_NAME": "바이엘코리아(주)",
                    }
                ],
            },
        )

    monkeypatch.setattr(external, "mfds_permission_search", truncated)
    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "ENFORCE")
    payload = run_external_tool_agent(
        "아일리아의 허가 품목명과 업체명을 공식 허가정보 기준으로 알려줘",
        resolver=BrandResolver(),
        external=external,
        provider=_no_tool_provider(),
    )

    ccs = payload["router_diagnostics"]["routing_v4"]["executed_call_signature"]
    assert ccs["reason_code"] == "TRUNCATED_RESULT"
    assert ccs["runtime_status"] == "typed_stop"
    assert "아일리아주사" not in payload["answer"]


def test_d09_duplicate_canonical_calls_never_execute(monkeypatch) -> None:
    external = ExternalApiClient(mode="fixture")
    executions = 0
    original = external.hira_disease_hospitalization_outpatient_stats

    def counted(*, sick_cd: str, year: str = "2024") -> ExternalCall:
        nonlocal executions
        executions += 1
        return original(sick_cd=sick_cd, year=year)

    duplicate = ProposedCall(
        tool_name="hira_disease_hospitalization_outpatient_stats",
        normalized_args={"sick_cd": "D69.3", "year": "2024"},
    )
    monkeypatch.setattr(external, "hira_disease_hospitalization_outpatient_stats", counted)
    monkeypatch.setattr(
        "jw_chat_agent_poc.tool_use.routing_v4_planner.classify_question",
        lambda _question: QuestionClassification(
            source_domain="hira",
            domain_decision_source=DomainDecisionSource.INTENT_OWNER,
            requested_capability="HIRA_DISEASE_PATIENT_STATS",
            direct_calls=(duplicate, duplicate),
            eligible_override=("hira_disease_hospitalization_outpatient_stats",),
        ),
    )
    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "ENFORCE")
    payload = run_external_tool_agent(
        "duplicate fixture",
        resolver=BrandResolver(),
        external=external,
        provider=_no_tool_provider(),
    )

    assert executions == 0
    assert payload["tool_calls"] == []
    ccs = payload["router_diagnostics"]["routing_v4"]["executed_call_signature"]
    assert ccs["reason_code"] == "INVALID_TOOL_ARGUMENTS"
