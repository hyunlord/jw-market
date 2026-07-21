from __future__ import annotations

from typing import Any

from jw_chat_agent_poc.service.runtime_provenance import trace_envelope


def _proposed_signature(mode: str) -> dict[str, Any]:
    return {
        "routing_mode": mode,
        "routing_decision": {
            "source_domain": "hira",
            "domain_decision_source": "PREFIX_RULE",
            "capability_status": "SUPPORTED",
            "tool_selection_source": "NEW_RULE",
            "route_outcome": "CALL",
        },
        "proposed_calls": [
            {
                "tool_name": "hira_disease_hospitalization_outpatient_stats",
                "normalized_args": {"sick_cd": "D69.3", "year": "2024"},
            }
        ],
    }


def _trace(result: dict[str, Any], *, answer: str, session_id: str) -> dict[str, Any]:
    return trace_envelope(
        question="상병코드 D693의 2024년 환자수",
        result=result,
        answer=answer,
        charts=(),
        timing={"stages": []},
        conversation_id=session_id,
    )


def _budget_trace(*, executed: int | None) -> dict[str, Any]:
    return {
        "contract_version": "external_tool_routing_v4_budget_v1",
        "planner_initial_call_cap": 1,
        "planner_repair_call_cap": 1,
        "planner_calls_used": 0,
        "planner_timeout_s": 20.0,
        "planner_token_cap": 512,
        "authority_tool_call_cap": 1,
        "authority_tool_calls_planned": 1,
        "authority_tool_calls_executed": executed,
        "duplicate_canonical_call_cap": 0,
        "official_web_fallback_call_cap": 0,
        "tool_call_timeouts": [
            {
                "call_ordinal": 1,
                "tool_name": "hira_disease_hospitalization_outpatient_stats",
                "timeout_s": 13.0,
            }
        ],
        "planner_latency_ms": 1.25,
        "tool_execution_latency_ms": None if executed is None else 2.5,
        "routing_latency_ms": 3.75,
    }


def test_trace_envelope_projects_v4_prs_without_ccs_in_shadow() -> None:
    proposed_signature = _proposed_signature("SHADOW")
    result = {
        "router_diagnostics": {
            "mode": "tool_use_agent",
            "routing_v4": {
                "routing_mode": "SHADOW",
                "proposed_routing_signature": proposed_signature,
                "eligible_tools": ["hira_disease_hospitalization_outpatient_stats"],
                "reason_code": None,
                "repair_count": 0,
                "deterministic_rule_id": "direct-disease-code",
                "budget": _budget_trace(executed=None),
                "shadow_status": "ok",
                "legacy_response_invariant": {
                    "before_sha256": "a" * 64,
                    "after_sha256": "a" * 64,
                    "unchanged": True,
                },
            },
        },
        "tool_calls": [],
        "markdown_response": {"fact_md": "", "data_md": ""},
    }

    trace = _trace(result, answer="기존 경로 응답", session_id="qa-shadow-session")

    routing_v4 = trace["qa_trace"]["routing_v4"]
    assert routing_v4["routing_mode"] == "SHADOW"
    assert routing_v4["proposed_routing_signature"] == proposed_signature
    assert "executed_call_signature" not in routing_v4
    assert routing_v4["shadow_status"] == "ok"
    assert routing_v4["budget"] == _budget_trace(executed=None)
    assert routing_v4["legacy_response_invariant"] == {
        "before_sha256": "a" * 64,
        "after_sha256": "a" * 64,
        "unchanged": True,
    }


def test_trace_envelope_projects_v4_ccs_in_enforce() -> None:
    proposed_signature = _proposed_signature("ENFORCE")
    executed_signature = {
        **proposed_signature,
        "executed_calls": [
            {
                "call_ordinal": 1,
                "parent_ordinal": None,
                "tool_name": "hira_disease_hospitalization_outpatient_stats",
                "normalized_args": {"sick_cd": "D69.3", "year": "2024"},
                "result_status": "ok",
            }
        ],
        "fallback_reason": None,
        "reason_code": None,
        "runtime_status": "ok",
    }
    result = {
        "router_diagnostics": {
            "mode": "tool_use_agent",
            "routing_v4": {
                "routing_mode": "ENFORCE",
                "proposed_routing_signature": proposed_signature,
                "executed_call_signature": executed_signature,
                "eligible_tools": ["hira_disease_hospitalization_outpatient_stats"],
                "reason_code": None,
                "repair_count": 0,
                "deterministic_rule_id": "direct-disease-code",
                "budget": _budget_trace(executed=1),
                "claim_evidence_binding_status": "pass",
                "claim_evidence_bindings": [
                    {
                        "claim_ordinal": 1,
                        "tool_name": "hira_disease_hospitalization_outpatient_stats",
                        "evidence_ids": ["hira:D69.3:2024"],
                    }
                ],
            },
        },
        "tool_calls": [],
        "markdown_response": {"fact_md": "", "data_md": ""},
    }

    trace = _trace(
        result,
        answer="- 2024년 환자수는 근거에 연결됐습니다.",
        session_id="qa-enforce-session",
    )

    routing_v4 = trace["qa_trace"]["routing_v4"]
    assert routing_v4["proposed_routing_signature"] == proposed_signature
    assert routing_v4["executed_call_signature"] == executed_signature
    assert routing_v4["budget"] == _budget_trace(executed=1)
    assert routing_v4["claim_evidence_binding_status"] == "pass"
    assert routing_v4["claim_evidence_bindings"] == (
        {
            "claim_ordinal": 1,
            "tool_name": "hira_disease_hospitalization_outpatient_stats",
            "evidence_ids": ("hira:D69.3:2024",),
        },
    )


def test_trace_rejects_budget_cap_that_does_not_match_planned_call_count() -> None:
    budget = _budget_trace(executed=1)
    budget["authority_tool_call_cap"] = 5
    result = {
        "router_diagnostics": {
            "routing_v4": {
                "routing_mode": "ENFORCE",
                "budget": budget,
            }
        },
        "tool_calls": [],
        "markdown_response": {"fact_md": "", "data_md": ""},
    }

    trace = _trace(result, answer="typed response", session_id="qa-invalid-budget")

    routing_v4 = trace["qa_trace"]["routing_v4"]
    assert routing_v4["budget_status"] == "invalid"
    assert "budget" not in routing_v4
