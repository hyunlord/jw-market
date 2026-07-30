from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
import time
from typing import Any

import pytest
import requests

from jw_chat_agent_poc.resolver import BrandResolver
from jw_chat_agent_poc.tool_use import integration as routing_integration
from jw_chat_agent_poc.tool_use import routing_v4_execution
from jw_chat_agent_poc.tool_use.contracts import AgentResult
from jw_chat_agent_poc.tool_use.integration import run_external_tool_agent
from jw_chat_agent_poc.tool_use.provider import ToolChoice
from jw_chat_agent_poc.tool_use.routing_v4_execution import claim_evidence_bindings
from jw_chat_agent_poc.tool_use.routing_v4_rules import QuestionClassification
from jw_chat_agent_poc.tool_use.routing_v4_types import (
    DomainDecisionSource,
    ProposedCall,
)
from jw_chat_agent_poc.tools.external import ExternalApiClient
from jw_chat_agent_poc.tools.external import ExternalCall
from jw_chat_agent_poc.tools.external.hira_reimbursement import (
    CacheLookupStatus,
    CacheStatus,
    HiraReimbursementHttpClient,
    MariaDbReimbursementStore,
    ReimbursementCacheResult,
    ReimbursementCriterion,
)


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


class _SlowProvider:
    def choose(self, *, user_text: str, messages: list[dict], tools: list[dict]) -> ToolChoice:
        del user_text, messages, tools
        time.sleep(0.25)
        return ToolChoice(None, {}, "late shadow response", call_id=None)


@dataclass(slots=True)
class _DelayedNoToolProvider:
    delay_seconds: float

    def choose(self, *, user_text: str, messages: list[dict], tools: list[dict]) -> ToolChoice:
        del user_text, messages, tools
        time.sleep(self.delay_seconds)
        return ToolChoice(None, {}, "no tool", call_id=None)


def _no_tool_provider(message: str = "no matching tool") -> _ChoiceSequence:
    return _ChoiceSequence((ToolChoice(None, {}, message, call_id=None),))


def _without_v4_diagnostics(payload: dict[str, Any]) -> dict[str, Any]:
    copied = dict(payload)
    diagnostics = dict(copied["router_diagnostics"])
    diagnostics.pop("routing_v4", None)
    copied["router_diagnostics"] = diagnostics
    return copied


def _enable_recoverable_reimbursement_cache(monkeypatch) -> None:
    monkeypatch.setenv("CHAT_CACHE_DB_HOST", "cache.internal")
    monkeypatch.setenv("CHAT_CACHE_DB_USER", "chat")
    monkeypatch.setenv("CHAT_CACHE_DB_PASSWORD", "masked-test-value")
    monkeypatch.setenv("CHAT_REIMBURSEMENT_DB_NAME", "reimbursement_stage")
    monkeypatch.setattr(
        MariaDbReimbursementStore,
        "get_reimbursement_criteria",
        lambda self, _brand: ReimbursementCacheResult(
            CacheStatus.NOT_FOUND,
            None,
            None,
            lookup_status=CacheLookupStatus.ZERO_ROWS,
            schema_name=self.schema_name,
        ),
    )


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
    budget = shadow["router_diagnostics"]["routing_v4"]["budget"]
    assert budget["planner_initial_call_cap"] == 1
    assert budget["planner_repair_call_cap"] == 1
    assert budget["planner_calls_used"] == 0
    assert budget["authority_tool_call_cap"] == 5
    assert budget["authority_tool_calls_planned"] == 5
    assert budget["authority_tool_calls_executed"] is None
    assert len(budget["tool_call_timeouts"]) == 5


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


def test_shadow_slow_provider_cannot_delay_the_legacy_response(monkeypatch) -> None:
    question = "당뇨병성 황반부종(DME) 관련 임상시험을 찾아줘"
    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "OFF")
    off = run_external_tool_agent(
        question,
        resolver=BrandResolver(),
        external=ExternalApiClient(mode="fixture"),
        provider=_no_tool_provider(),
    )
    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "SHADOW")
    monkeypatch.setenv("CHAT_TOOL_ROUTING_SHADOW_MAX_WAIT_MS", "25")

    started = time.monotonic()
    shadow = run_external_tool_agent(
        question,
        resolver=BrandResolver(),
        external=ExternalApiClient(mode="fixture"),
        provider=_no_tool_provider(),
        routing_provider=_SlowProvider(),
    )
    elapsed = time.monotonic() - started

    assert elapsed < 0.10
    assert _without_v4_diagnostics(shadow) == off
    diagnostics = shadow["router_diagnostics"]["routing_v4"]
    assert diagnostics["shadow_status"] == "budget_exceeded"


def test_shadow_planner_uses_legacy_execution_time_before_spending_response_budget(monkeypatch) -> None:
    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "SHADOW")
    monkeypatch.setenv("CHAT_TOOL_ROUTING_SHADOW_MAX_WAIT_MS", "25")

    payload = run_external_tool_agent(
        "당뇨병성 황반부종(DME) 관련 임상시험을 찾아줘",
        resolver=BrandResolver(),
        external=ExternalApiClient(mode="fixture"),
        provider=_DelayedNoToolProvider(delay_seconds=0.08),
        routing_provider=_DelayedNoToolProvider(delay_seconds=0.06),
    )

    assert payload["router_diagnostics"]["routing_v4"]["shadow_status"] == "ok"


def test_shadow_internal_mart_path_emits_prs_and_same_request_invariant(monkeypatch) -> None:
    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "SHADOW")
    payload = {
        "question": "리바로 매출 추이",
        "router_diagnostics": {"mode": "strategic_market"},
        "tool_calls": [{"tool": "get_brand_metric", "status": "ok"}],
        "answer": "리바로 매출은 2026-05 기준 10억원입니다.",
        "markdown_response": {"markdown": "리바로 매출은 2026-05 기준 10억원입니다."},
        "sources": ["UBIST"],
    }

    observed = routing_integration.attach_routing_v4_legacy_observation(
        "리바로 매출 추이",
        payload,
    )

    diagnostics = observed["router_diagnostics"]["routing_v4"]
    decision = diagnostics["proposed_routing_signature"]["routing_decision"]
    assert decision == {
        "source_domain": "internal_mart",
        "domain_decision_source": "METRIC_OWNER",
        "capability_status": "SUPPORTED",
        "tool_selection_source": "LEGACY_RULE",
        "route_outcome": "CALL",
    }
    assert diagnostics["proposed_routing_signature"]["proposed_calls"] == []
    invariant = diagnostics["legacy_response_invariant"]
    assert invariant["before_sha256"] == invariant["after_sha256"]
    assert invariant["unchanged"] is True


def test_shadow_actual_internal_mart_call_overrides_external_intent_guess(monkeypatch) -> None:
    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "SHADOW")
    monkeypatch.setattr(
        routing_integration,
        "classify_question",
        lambda _question: QuestionClassification(
            source_domain="hira",
            domain_decision_source=DomainDecisionSource.INTENT_OWNER,
            requested_capability="HIRA_DISEASE_PATIENT_STATS",
        ),
    )
    payload = {
        "question": "고지혈증 시장 매출 알려줘",
        "router_diagnostics": {"mode": "agent_loop"},
        "tool_calls": [{"tool": "get_brand_metric", "status": "ok"}],
        "answer": "고지혈증 시장 매출은 100억원입니다.",
        "sources": ["UBIST"],
    }

    observed = routing_integration.attach_routing_v4_legacy_observation(
        "고지혈증 시장 매출 알려줘",
        payload,
    )

    decision = observed["router_diagnostics"]["routing_v4"]["proposed_routing_signature"][
        "routing_decision"
    ]
    assert decision["source_domain"] == "internal_mart"
    assert decision["domain_decision_source"] == "METRIC_OWNER"


def test_shadow_external_catalog_call_is_not_labeled_internal_mart(monkeypatch) -> None:
    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "SHADOW")
    payload = {
        "question": "최신 치료 가이드라인을 찾아줘",
        "router_diagnostics": {"mode": "agent_loop"},
        "tool_calls": [{"tool": "web_search", "status": "ok"}],
        "answer": "검색 결과입니다.",
        "sources": ["web"],
    }

    observed = routing_integration.attach_routing_v4_legacy_observation(
        "최신 치료 가이드라인을 찾아줘",
        payload,
    )

    assert "routing_v4" not in observed["router_diagnostics"]


def test_enforce_internal_mart_path_emits_ccs_without_external_execution(monkeypatch) -> None:
    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "ENFORCE")
    payload = {
        "question": "리바로 매출 추이",
        "router_diagnostics": {"mode": "strategic_market"},
        "tool_calls": [{"tool": "get_brand_metric", "status": "ok"}],
        "answer": "리바로 매출은 2026-05 기준 10억원입니다.",
        "markdown_response": {"markdown": "리바로 매출은 2026-05 기준 10억원입니다."},
        "sources": ["UBIST"],
    }

    observed = routing_integration.attach_routing_v4_legacy_observation(
        "리바로 매출 추이",
        payload,
    )

    diagnostics = observed["router_diagnostics"]["routing_v4"]
    ccs = diagnostics["executed_call_signature"]
    assert ccs["routing_decision"]["source_domain"] == "internal_mart"
    assert ccs["routing_decision"]["route_outcome"] == "CALL"
    assert ccs["proposed_calls"] == []
    assert ccs["executed_calls"] == []
    assert diagnostics["claim_evidence_binding_status"] == "not_applicable"
    assert diagnostics["budget"]["authority_tool_call_cap"] == 0
    assert diagnostics["budget"]["authority_tool_calls_planned"] == 0
    assert diagnostics["budget"]["authority_tool_calls_executed"] == 0


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


def test_enforce_routes_nct_id_directly_to_detail_tool(monkeypatch) -> None:
    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "ENFORCE")

    payload = run_external_tool_agent(
        "NCT05151731의 결과와 선정기준을 알려줘",
        resolver=BrandResolver(),
        external=ExternalApiClient(mode="fixture"),
        provider=_no_tool_provider(),
        routing_provider=_TimeoutProvider(),
    )

    assert [call["tool"] for call in payload["tool_calls"]] == [
        "clinicaltrials_study_details"
    ]
    assert "선정·제외기준은 현재 연결에서 앞부분 200자까지만 제공됩니다" in payload["answer"]
    proposal = payload["router_diagnostics"]["routing_v4"]["proposed_routing_signature"]
    assert proposal["routing_decision"]["tool_selection_source"] == "NEW_RULE"
    assert proposal["proposed_calls"] == [
        {
            "tool_name": "clinicaltrials_study_details",
            "normalized_args": {"nct_id": "NCT05151731"},
        }
    ]


def test_enforce_supplements_partial_nct_design_with_official_web(monkeypatch) -> None:
    external = ExternalApiClient(mode="fixture")
    web_calls = 0

    def partial_detail(nct_id: str) -> ExternalCall:
        return ExternalCall(
            tool="clinicaltrials_study_details",
            source="clinicaltrials_mcp",
            status="live",
            summary_text=f"ClinicalTrials.gov에서 {nct_id} 일부 상세를 확인했습니다.",
            render_data={
                "detail": {
                    "nct_id": nct_id,
                    "enrollment": 300,
                    "outcomes": ["Visual acuity"],
                }
            },
            safe_url=f"https://clinicaltrials.gov/study/{nct_id}",
        )

    def web_search(
        query: str,
        max_results: int = 5,
        *,
        topic: str = "general",
    ) -> ExternalCall:
        nonlocal web_calls
        del query, max_results, topic
        web_calls += 1
        return ExternalCall(
            tool="web_search",
            source="web_search",
            status="ok",
            summary_text="official clinical supplement",
            render_data={
                "provider": "fixture",
                "items": [
                    {
                        "title": "NCT05151731 Study Details",
                        "url": "https://clinicaltrials.gov/study/NCT05151731",
                    }
                ],
            },
        )

    monkeypatch.setattr(external, "clinicaltrials_study_details", partial_detail)
    monkeypatch.setattr(external, "web_search", web_search)
    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "ENFORCE")
    monkeypatch.setenv("CHAT_TOOL_ROUTING_OFFICIAL_WEB_FALLBACK_ENABLED", "true")

    payload = run_external_tool_agent(
        "NCT05151731 시험 디자인 알려줘",
        resolver=BrandResolver(),
        external=external,
        provider=_no_tool_provider(),
        routing_provider=_TimeoutProvider(),
    )

    diagnostics = payload["router_diagnostics"]["routing_v4"]
    signature = diagnostics["executed_call_signature"]
    fallback = diagnostics["official_web_fallback"]
    assert signature["runtime_status"] == "partial"
    assert signature["reason_code"] == "PARTIAL_RESULT"
    assert fallback["missing_requested_facets"] == [
        "start_date",
        "primary_completion_date",
        "allocation",
        "masking",
        "intervention_model",
    ]
    assert fallback["calls_executed"] == 1
    assert fallback["accepted_urls"] == [
        "https://clinicaltrials.gov/study/NCT05151731"
    ]
    assert web_calls == 1
    assert "공식 웹 보완 자료" in payload["answer"]


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
    budget = diagnostics["budget"]
    assert budget["planner_calls_used"] == 0
    assert budget["planner_token_cap"] == 512
    assert budget["authority_tool_call_cap"] == 1
    assert budget["authority_tool_calls_planned"] == 1
    assert budget["authority_tool_calls_executed"] == 1
    assert budget["tool_call_timeouts"][0]["tool_name"] == "mfds_permission_search"


def test_enforce_executes_raw_validated_arguments_but_records_normalized_ccs(monkeypatch) -> None:
    external = ExternalApiClient(mode="fixture")
    observed_queries: list[str] = []

    def clinical_search(query_intr: str, *, query_type: str = "intervention") -> ExternalCall:
        observed_queries.append(query_intr)
        return ExternalCall(
            tool="clinicaltrials_v2_search",
            source="clinicaltrials_mcp",
            status="live",
            summary_text="one result",
            render_data={
                "payload": {
                    "studies": [
                        {
                            "NCTId": "NCT00000001",
                            "briefTitle": "DME trial",
                            "overallStatus": "RECRUITING",
                        }
                    ]
                },
                "request": {"query": query_intr, "query_type": query_type},
            },
        )

    provider = _ChoiceSequence(
        (
            ToolChoice(
                "clinicaltrials_v2_search",
                {"query": "  diabetic   macular edema  ", "query_type": "condition"},
                "bounded query",
                call_id="proposal-raw",
            ),
        )
    )
    monkeypatch.setattr(external, "clinicaltrials_v2_search", clinical_search)
    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "ENFORCE")

    payload = run_external_tool_agent(
        "당뇨병성 황반부종 관련 임상시험을 찾아줘",
        resolver=BrandResolver(),
        external=external,
        provider=_no_tool_provider(),
        routing_provider=provider,
    )

    assert observed_queries == ["  diabetic   macular edema  "]
    diagnostics = payload["router_diagnostics"]["routing_v4"]
    assert diagnostics["input_key"] == "natural_query"
    assert diagnostics["eligible_tools_count"] == 2
    assert diagnostics["executed_call_signature"]["executed_calls"][0]["normalized_args"] == {
        "query": "diabetic macular edema",
        "query_type": "condition",
    }


def test_enforce_reimbursement_uses_hira_criteria(monkeypatch) -> None:
    external = ExternalApiClient(mode="fixture")
    _enable_recoverable_reimbursement_cache(monkeypatch)

    def reimbursement(_self, brand: str) -> ReimbursementCriterion:
        assert brand == "아일리아"
        return ReimbursementCriterion(
            brand_name=brand,
            title="항혈관내피성장인자 주사제 급여기준",
            raw_text="신생혈관성 연령관련 황반변성 급여 기준",
            source_date="2026-06-24",
            collected_at=datetime(2026, 7, 25, tzinfo=UTC),
            notice_number="보건복지부 고시 제2026-101호",
            source_url="https://www.hira.or.kr/rc/example.do",
        )

    monkeypatch.setattr(HiraReimbursementHttpClient, "fetch", reimbursement)
    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "ENFORCE")

    payload = run_external_tool_agent(
        "아일리아의 급여기준에 대해서 적응증 별로 설명해줘",
        resolver=BrandResolver(),
        external=external,
        provider=_no_tool_provider(),
        routing_provider=_TimeoutProvider(),
    )

    assert [call["tool"] for call in payload["tool_calls"]] == ["hira_reimbursement_criteria"]
    assert "신생혈관성 연령관련 황반변성 급여 기준" in payload["answer"]
    evidence = payload["tool_calls"][0]["render_data"]["evidence"]
    assert [fact["source_name"] for fact in evidence] == ["심사평가원(HIRA) 보험인정기준"]
    ccs = payload["router_diagnostics"]["routing_v4"]["executed_call_signature"]
    assert ccs["routing_decision"]["capability_status"] == "SUPPORTED"
    assert ccs["routing_decision"]["route_outcome"] == "CALL"
    assert ccs["reason_code"] is None
    assert ccs["executed_calls"][0]["tool_name"] == "hira_reimbursement_criteria"


def test_enforce_label_fields_use_nedrug_permission_detail(monkeypatch) -> None:
    external = ExternalApiClient(mode="fixture")

    def permission_search(brand: str) -> ExternalCall:
        assert brand == "아일리아"
        return ExternalCall(
            tool="mfds_permission_search",
            source="external_api",
            status="ok",
            summary_text="아일리아 허가 품목 1건",
            render_data={
                "resultCode": "00",
                "items": [{"ITEM_SEQ": "201306324", "ITEM_NAME": "아일리아주사"}],
            },
        )

    def permission_detail(item_seq: str) -> ExternalCall:
        assert item_seq == "201306324"
        return ExternalCall(
            tool="mfds_permission_detail",
            source="external_api",
            status="ok",
            summary_text="아일리아 허가 상세 1건",
            render_data={
                "resultCode": "00",
                "items": [
                    {
                        "ITEM_SEQ": item_seq,
                        "ITEM_NAME": "아일리아주사",
                        "EE_DOC_DATA": "신생혈관성 연령관련 황반변성의 치료",
                        "UD_DOC_DATA": "4주마다 1회 유리체내 투여",
                        "WARNING_DOC_DATA": "안내염 징후를 관찰하십시오.",
                    }
                ],
            },
        )

    monkeypatch.setattr(external, "mfds_permission_search", permission_search)
    monkeypatch.setattr(external, "mfds_permission_detail", permission_detail)
    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "ENFORCE")

    payload = run_external_tool_agent(
        "NeDrug: 아일리아 제품의 효능·효과, 용법·용량, 사용상 주의사항을 알려줘",
        resolver=BrandResolver(),
        external=external,
        provider=_no_tool_provider(),
        routing_provider=_TimeoutProvider(),
    )

    assert [call["tool"] for call in payload["tool_calls"]] == ["mfds_permission_search"]
    assert "신생혈관성 연령관련 황반변성의 치료" in payload["answer"]
    assert "4주마다 1회 유리체내 투여" in payload["answer"]
    assert "안내염 징후를 관찰하십시오." in payload["answer"]
    proposal = payload["router_diagnostics"]["routing_v4"]["proposed_routing_signature"]
    assert proposal["proposed_calls"] == [
        {"tool_name": "mfds_permission_search", "normalized_args": {"brand": "아일리아"}}
    ]


def test_enforce_reimbursement_hira_absence_is_typed_no_evidence(monkeypatch) -> None:
    external = ExternalApiClient(mode="fixture")

    monkeypatch.setattr(HiraReimbursementHttpClient, "fetch", lambda _self, _brand: None)
    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "ENFORCE")

    payload = run_external_tool_agent(
        "아일리아의 급여기준에 대해서 적응증 별로 설명해줘",
        resolver=BrandResolver(),
        external=external,
        provider=_no_tool_provider(),
        routing_provider=_TimeoutProvider(),
    )

    assert [call["tool"] for call in payload["tool_calls"]] == ["hira_reimbursement_criteria"]
    assert "mart" not in payload["answer"].casefold()
    ccs = payload["router_diagnostics"]["routing_v4"]["executed_call_signature"]
    assert ccs["runtime_status"] == "typed_stop"
    assert ccs["reason_code"] == "NO_RECORD_FOUND"
    assert ccs["routing_decision"]["source_domain"] == "hira"


def test_enforce_reimbursement_empty_web_result_returns_actionable_guidance(
    monkeypatch,
) -> None:
    external = ExternalApiClient(mode="fixture")
    web_calls = 0

    def empty_web_result(
        query: str,
        max_results: int = 5,
        *,
        topic: str = "general",
    ) -> ExternalCall:
        nonlocal web_calls
        del query, max_results, topic
        web_calls += 1
        return ExternalCall(
            tool="web_search",
            source="web_search",
            status="no_data",
            summary_text="no official results",
            render_data={"provider": "fixture", "items": []},
        )

    _enable_recoverable_reimbursement_cache(monkeypatch)
    monkeypatch.setattr(HiraReimbursementHttpClient, "fetch", lambda _self, _brand: None)
    monkeypatch.setattr(external, "web_search", empty_web_result)
    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "ENFORCE")
    monkeypatch.setenv("CHAT_TOOL_ROUTING_OFFICIAL_WEB_FALLBACK_ENABLED", "true")

    payload = run_external_tool_agent(
        "아일리아 급여기준 알려줘",
        resolver=BrandResolver(),
        external=external,
        provider=_no_tool_provider(),
        routing_provider=_TimeoutProvider(),
    )

    fallback = payload["router_diagnostics"]["routing_v4"]["official_web_fallback"]
    assert web_calls == 1
    assert fallback["accepted_urls"] == []
    assert "공식 웹 보완 검색을 시도" in payload["answer"]
    assert "https://www.hira.or.kr/rc/insu/insuadtcrtr/InsuAdtCrtrList.do" in payload["answer"]
    assert "검색어: 아일리아 급여기준" in payload["answer"]


def test_enforce_reimbursement_identity_mismatch_keeps_reason_and_skips_web(
    monkeypatch,
) -> None:
    external = ExternalApiClient(mode="fixture")
    web_calls = 0

    def mismatched_reimbursement(_self, brand: str) -> ReimbursementCriterion:
        assert brand == "리바로"
        return ReimbursementCriterion(
            brand_name=brand,
            title="고지혈증 치료제 급여기준",
            raw_text=(
                "Ezetimibe + pitavastatin calcium 복합경구제"
                "(품명: 리바로젯정 등)"
            ),
            source_date="2021-10-01",
            collected_at=datetime(2026, 7, 28, tzinfo=UTC),
            notice_number="제2021-245호",
            source_url="https://www.hira.or.kr/rc/example.do",
        )

    def unexpected_web_search(*_args, **_kwargs) -> ExternalCall:
        nonlocal web_calls
        web_calls += 1
        raise AssertionError("identity mismatch must not promote web snippets")

    _enable_recoverable_reimbursement_cache(monkeypatch)
    monkeypatch.setattr(HiraReimbursementHttpClient, "fetch", mismatched_reimbursement)
    monkeypatch.setattr(external, "web_search", unexpected_web_search)
    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "ENFORCE")
    monkeypatch.setenv("CHAT_TOOL_ROUTING_OFFICIAL_WEB_FALLBACK_ENABLED", "true")

    payload = run_external_tool_agent(
        "리바로 급여기준 알려줘",
        resolver=BrandResolver(),
        external=external,
        provider=_no_tool_provider(),
        routing_provider=_TimeoutProvider(),
    )

    routing = payload["router_diagnostics"]["routing_v4"]
    assert web_calls == 0
    assert routing["executed_call_signature"]["reason_code"] == "IDENTITY_MISMATCH"
    assert routing["official_web_fallback"]["reason_code"] == "IDENTITY_MISMATCH"
    assert "제품 또는 성분 구성이 요청한 브랜드와 일치하지 않아" in payload["answer"]
    assert "검색어: 리바로" in payload["answer"]
    assert "리바로젯정" not in payload["answer"]


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


@pytest.mark.parametrize(
    ("question", "expected_sick_cd"),
    (
        ("상병코드 D693의 환자수 알려줘", "D69.3"),
        ("상병코드 D69.3의 환자수 알려줘", "D69.3"),
        ("질병코드 H360 환자수 통계 알려줘", "H36.0"),
        ("질병코드 H36.0 환자수 통계 알려줘", "H36.0"),
        ("상병코드 E11 환자수 알려줘", "E11"),
        ("상병코드 E11.3 환자수 알려줘", "E11.3"),
        ("상병코드 D69 환자수 알려줘", "D69"),
        ("상병코드 I10 환자수 알려줘", "I10"),
    ),
)
def test_s2_direct_kcd_routes_without_planner_tool_call(
    monkeypatch,
    question: str,
    expected_sick_cd: str,
) -> None:
    external = ExternalApiClient(mode="fixture")
    observed_codes: list[str] = []
    provider = _no_tool_provider()

    def no_rows(sick_cd: str, year: str = "2024") -> ExternalCall:
        observed_codes.append(sick_cd)
        return ExternalCall(
            tool="hira_disease_hospitalization_outpatient_stats",
            source="external_api",
            status="no_data",
            summary_text="no rows for exact disease code",
            render_data={
                "status": "no_data",
                "items": [],
                "request": {"sick_cd": sick_cd, "year": year},
            },
        )

    monkeypatch.setattr(external, "hira_disease_hospitalization_outpatient_stats", no_rows)
    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "ENFORCE")
    payload = run_external_tool_agent(
        question,
        resolver=BrandResolver(),
        external=external,
        provider=provider,
    )

    assert provider.calls == 0
    assert observed_codes == [expected_sick_cd]
    ccs = payload["router_diagnostics"]["routing_v4"]["executed_call_signature"]
    assert ccs["reason_code"] == "NO_RECORD_FOUND"
    assert ccs["runtime_status"] == "typed_stop"
    assert ccs["proposed_calls"] == [
        {
            "tool_name": "hira_disease_hospitalization_outpatient_stats",
            "normalized_args": {"sick_cd": expected_sick_cd, "year": "2024"},
        }
    ]


def test_a01_partial_periods_preserve_official_rows_without_trend_claim(monkeypatch) -> None:
    external = ExternalApiClient(mode="fixture")
    web_calls = 0

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

    def web_search(
        query: str,
        max_results: int = 5,
        *,
        topic: str = "general",
    ) -> ExternalCall:
        nonlocal web_calls
        del query, max_results, topic
        web_calls += 1
        return ExternalCall(
            tool="web_search",
            source="web_search",
            status="ok",
            summary_text="official supplement",
            render_data={
                "provider": "fixture",
                "items": [
                    {
                        "title": "HIRA 2022 statistics",
                        "url": "https://opendata.hira.or.kr/official-2022",
                    }
                ],
            },
        )

    monkeypatch.setattr(external, "hira_disease_hospitalization_outpatient_stats", period_rows)
    monkeypatch.setattr(external, "web_search", web_search)
    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "ENFORCE")
    monkeypatch.setenv("CHAT_TOOL_ROUTING_OFFICIAL_WEB_FALLBACK_ENABLED", "true")
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
    assert web_calls == 1
    assert [call["tool"] for call in payload["tool_calls"]].count("web_search") == 1
    assert "일부 결과" in payload["answer"]
    assert "2022" in payload["answer"]
    assert "공식 웹 보완 자료" in payload["answer"]
    assert payload["markdown_response"]["fact_md"]
    assert payload["markdown_response"]["verification"]["status"] == "partial"
    assert not any(token in payload["answer"] for token in ("연속 상승", "연속 하락", "반등", "정점"))


def test_a10_clinical_search_discloses_retrieved_displayed_and_upstream_total(monkeypatch) -> None:
    external = ExternalApiClient(mode="fixture")
    studies = [
        {"NCTId": f"NCT0000000{index}", "briefTitle": f"DME study {index}"}
        for index in range(1, 8)
    ]

    def search(query_intr: str, *, query_type: str = "intervention") -> ExternalCall:
        assert query_intr == "diabetic macular edema"
        assert query_type == "condition"
        return ExternalCall(
            tool="clinicaltrials_v2_search",
            source="external_api",
            status="ok",
            summary_text="7 retrieved from 700",
            render_data={
                "items": studies,
                "payload": {"totalCount": 700},
                "request": {"query.condition": query_intr},
            },
        )

    provider = _ChoiceSequence(
        (
            ToolChoice(
                "clinicaltrials_v2_search",
                {"query": "diabetic macular edema", "query_type": "condition"},
                "global clinical source",
                call_id="a10-1",
            ),
        )
    )
    monkeypatch.setattr(external, "clinicaltrials_v2_search", search)
    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "ENFORCE")
    payload = run_external_tool_agent(
        "당뇨병성 황반부종(DME) 관련 임상시험을 찾아줘",
        resolver=BrandResolver(),
        external=external,
        provider=_no_tool_provider(),
        routing_provider=provider,
    )

    assert "현재 연결 조회 건수 = 7건" in payload["answer"]
    assert "표시 건수 = 5건" in payload["answer"]
    assert "원천 제공 총 건수 = 700건" in payload["answer"]


def test_a10_does_not_invent_total_when_upstream_omits_it(monkeypatch) -> None:
    external = ExternalApiClient(mode="fixture")

    def search(query_intr: str, *, query_type: str = "intervention") -> ExternalCall:
        return ExternalCall(
            tool="clinicaltrials_v2_search",
            source="external_api",
            status="ok",
            summary_text="one retrieved",
            render_data={
                "items": [{"NCTId": "NCT00000001", "briefTitle": query_intr}],
                "request": {"query_type": query_type},
            },
        )

    provider = _ChoiceSequence(
        (
            ToolChoice(
                "clinicaltrials_v2_search",
                {"query": "diabetic macular edema", "query_type": "condition"},
                "global clinical source",
                call_id="a10-1",
            ),
        )
    )
    monkeypatch.setattr(external, "clinicaltrials_v2_search", search)
    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "ENFORCE")
    payload = run_external_tool_agent(
        "당뇨병성 황반부종(DME) 관련 임상시험을 찾아줘",
        resolver=BrandResolver(),
        external=external,
        provider=_no_tool_provider(),
        routing_provider=provider,
    )

    assert "현재 연결 조회 건수 = 1건" in payload["answer"]
    assert "표시 건수 = 1건" in payload["answer"]
    assert "원천 제공 총 건수" not in payload["answer"]


def test_s7_clinicaltrials_prefix_runs_dme_condition_list_without_provider(monkeypatch) -> None:
    external = ExternalApiClient(mode="fixture")
    studies = [
        {"NCTId": f"NCT0000000{index}", "briefTitle": f"DME study {index}"}
        for index in range(1, 8)
    ]

    def search(query_intr: str, *, query_type: str = "intervention") -> ExternalCall:
        assert query_intr == "diabetic macular edema"
        assert query_type == "condition"
        return ExternalCall(
            tool="clinicaltrials_v2_search",
            source="external_api",
            status="ok",
            summary_text="7 retrieved from 700",
            render_data={
                "items": studies,
                "payload": {"totalCount": 700},
                "request": {"query.condition": query_intr},
            },
        )

    monkeypatch.setattr(external, "clinicaltrials_v2_search", search)
    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "ENFORCE")
    payload = run_external_tool_agent(
        "ClinicalTrials: 당뇨황반부종(DME) 질환의 임상·허가심사 단계 경쟁약물 현황",
        resolver=BrandResolver(),
        external=external,
        provider=_no_tool_provider(),
        routing_provider=_TimeoutProvider(),
    )

    tools = [call["tool"] for call in payload["tool_calls"]]
    assert tools == ["clinicaltrials_v2_search"]
    assert payload["router_diagnostics"]["routing_v4"]["budget"]["planner_calls_used"] == 0
    assert "NCT00000001" in payload["answer"]
    assert "NCT00000005" in payload["answer"]
    assert "NCT00000006" not in payload["answer"]
    assert "현재 연결 조회 건수 = 7건" in payload["answer"]
    assert "표시 건수 = 5건" in payload["answer"]
    assert "원천 제공 총 건수 = 700건" in payload["answer"]
    assert "등록 목록만 표시합니다" in payload["answer"]
    assert "의약품별 집계, 순위, 경쟁 분석/서사는 제공하지 않습니다" in payload["answer"]
    assert not any(
        phrase in payload["answer"]
        for phrase in ("1위", "상위 후보", "경쟁 우위", "가장 유망", "시장 선도")
    )


def test_s7_clinicaltrials_zero_rows_returns_typed_absence(monkeypatch) -> None:
    external = ExternalApiClient(mode="fixture")

    def search(query_intr: str, *, query_type: str = "intervention") -> ExternalCall:
        return ExternalCall(
            tool="clinicaltrials_v2_search",
            source="external_api",
            status="ok",
            summary_text="0 retrieved",
            render_data={
                "items": [],
                "payload": {"totalCount": 0},
                "request": {"query.condition": query_intr, "query_type": query_type},
            },
        )

    monkeypatch.setattr(external, "clinicaltrials_v2_search", search)
    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "ENFORCE")
    payload = run_external_tool_agent(
        "ClinicalTrials: 당뇨황반부종(DME) 질환의 임상·허가심사 단계 경쟁약물 현황",
        resolver=BrandResolver(),
        external=external,
        provider=_no_tool_provider(),
        routing_provider=_TimeoutProvider(),
    )

    assert payload["agent_loop_metrics"]["status"] == "typed_stop"
    assert payload["router_diagnostics"]["routing_v4"]["executed_call_signature"]["reason_code"] == "NO_RECORD_FOUND"
    assert [call["tool"] for call in payload["tool_calls"]] == ["clinicaltrials_v2_search"]
    assert payload["answer"].startswith("상태: 확인 불가")
    assert "ClinicalTrials.gov 질환 조건 검색 결과가 0건입니다" in payload["answer"]
    assert "등록 목록만 표시합니다" in payload["answer"]


def test_s7_clinicaltrials_error_returns_typed_absence(monkeypatch) -> None:
    external = ExternalApiClient(mode="fixture")

    def search(query_intr: str, *, query_type: str = "intervention") -> ExternalCall:
        return ExternalCall(
            tool="clinicaltrials_v2_search",
            source="external_api",
            status="error",
            summary_text="gateway failed",
            render_data={
                "message": "injected failure",
                "request": {"query.condition": query_intr, "query_type": query_type},
            },
        )

    monkeypatch.setattr(external, "clinicaltrials_v2_search", search)
    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "ENFORCE")
    payload = run_external_tool_agent(
        "ClinicalTrials: 당뇨황반부종(DME) 질환의 임상·허가심사 단계 경쟁약물 현황",
        resolver=BrandResolver(),
        external=external,
        provider=_no_tool_provider(),
        routing_provider=_TimeoutProvider(),
    )

    assert payload["agent_loop_metrics"]["status"] == "typed_stop"
    signature = payload["router_diagnostics"]["routing_v4"]["executed_call_signature"]
    assert signature["reason_code"] == "UPSTREAM_UNAVAILABLE"
    assert payload["answer"].startswith("상태: 확인 불가")
    assert "ClinicalTrials.gov 조회에 실패했습니다" in payload["answer"]
    assert "등록 목록만 표시합니다" in payload["answer"]


def test_d13_mutated_rendered_claim_fails_evidence_binding() -> None:
    result = AgentResult(
        status="ok",
        answer="- 아일리아: 허가 품목 = 조작된 품목 [식약처 의약품 정보]",
        tool_calls=(
            {
                "tool": "mfds_permission_search",
                "status": "ok",
                "render_data": {
                    "evidence": [
                        {
                            "fact_id": "mfds_permission_search:1",
                            "subject": "아일리아",
                            "metric": "허가 품목",
                            "value": None,
                            "unit": None,
                            "period": None,
                            "source_name": "식약처 의약품 정보",
                            "source_locator": "아일리아주사 · 바이엘코리아(주)",
                            "raw_ref": "mfds_permission_search:1",
                        }
                    ]
                },
            },
        ),
        sources=("식약처 의약품 정보",),
        traces=(),
        fallback_code=None,
    )

    status, bindings = claim_evidence_bindings(result)

    assert status == "fail"
    assert bindings == []


def test_d06_authoritative_timeout_stops_without_web_fallback(monkeypatch) -> None:
    external = ExternalApiClient(mode="fixture")

    def timeout(sick_cd: str, *, year: str = "2024") -> ExternalCall:
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


def test_d06c_runtime_uses_one_allowlisted_web_fallback_after_hira_outage(
    monkeypatch,
) -> None:
    external = ExternalApiClient(mode="fixture")
    web_calls = 0

    def timeout(sick_cd: str, *, year: str = "2024") -> ExternalCall:
        del sick_cd, year
        raise requests.Timeout("authoritative timeout")

    def web_search(
        query: str,
        max_results: int = 5,
        *,
        topic: str = "general",
    ) -> ExternalCall:
        nonlocal web_calls
        del query, max_results, topic
        web_calls += 1
        return ExternalCall(
            tool="web_search",
            source="web_search",
            status="ok",
            summary_text="official and unverified web rows",
            render_data={
                "provider": "fixture",
                "items": [
                    {
                        "title": "HIRA official statistics page",
                        "url": "https://opendata.hira.or.kr/official",
                    },
                    {
                        "title": "unverified blog",
                        "url": "https://blog.naver.com/unverified-statistics",
                    },
                ],
            },
        )

    monkeypatch.setattr(external, "hira_disease_hospitalization_outpatient_stats", timeout)
    monkeypatch.setattr(external, "web_search", web_search)
    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "ENFORCE")
    monkeypatch.setenv("CHAT_TOOL_ROUTING_OFFICIAL_WEB_FALLBACK_ENABLED", "true")

    payload = run_external_tool_agent(
        "상병코드 D693의 최근 5개년 환자수 추이를 분석해줘",
        resolver=BrandResolver(),
        external=external,
        provider=_no_tool_provider(),
    )

    assert web_calls == 1
    assert [call["tool"] for call in payload["tool_calls"]].count("web_search") == 1
    assert "공식 웹 보완 자료" in payload["answer"]
    assert "https://opendata.hira.or.kr/official" in payload["answer"]
    assert "blog.naver.com" not in payload["answer"]
    fallback = payload["router_diagnostics"]["routing_v4"]["official_web_fallback"]
    assert fallback["calls_executed"] == 1
    assert fallback["accepted_urls"] == ["https://opendata.hira.or.kr/official"]
    assert fallback["call_cap"] == 1
    assert fallback["retry_cap"] == 0
    assert fallback["timeout_s"] == 5.0


def test_d06c_explicit_hira_request_never_executes_web_fallback(monkeypatch) -> None:
    external = ExternalApiClient(mode="fixture")
    web_calls = 0

    def timeout(sick_cd: str, *, year: str = "2024") -> ExternalCall:
        del sick_cd, year
        raise requests.Timeout("authoritative timeout")

    def web_search(
        query: str,
        max_results: int = 5,
        *,
        topic: str = "general",
    ) -> ExternalCall:
        nonlocal web_calls
        del query, max_results, topic
        web_calls += 1
        raise AssertionError("disabled fallback must not execute")

    monkeypatch.setattr(external, "hira_disease_hospitalization_outpatient_stats", timeout)
    monkeypatch.setattr(external, "web_search", web_search)
    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "ENFORCE")
    monkeypatch.delenv("CHAT_TOOL_ROUTING_OFFICIAL_WEB_FALLBACK_ENABLED", raising=False)

    payload = run_external_tool_agent(
        "HIRA: 상병코드 D693의 최근 5개년 환자수 추이를 분석해줘",
        resolver=BrandResolver(),
        external=external,
        provider=_no_tool_provider(),
    )

    assert web_calls == 0
    assert "web_search" not in [call["tool"] for call in payload["tool_calls"]]
    assert payload["router_diagnostics"]["routing_v4"]["executed_call_signature"][
        "reason_code"
    ] == "UPSTREAM_UNAVAILABLE"


def test_d06c_explicit_hira_failure_uses_disclosed_web_supplement(monkeypatch) -> None:
    external = ExternalApiClient(mode="fixture")
    web_calls = 0

    def timeout(sick_cd: str, *, year: str = "2024") -> ExternalCall:
        del sick_cd, year
        raise requests.Timeout("authoritative timeout")

    def web_search(
        query: str,
        max_results: int = 5,
        *,
        topic: str = "general",
    ) -> ExternalCall:
        nonlocal web_calls
        del query, max_results, topic
        web_calls += 1
        return ExternalCall(
            tool="web_search",
            source="web_search",
            status="ok",
            summary_text="official supplement",
            render_data={
                "provider": "fixture",
                "items": [
                    {
                        "title": "HIRA official statistics page",
                        "url": "https://opendata.hira.or.kr/official",
                    }
                ],
            },
        )

    monkeypatch.setattr(external, "hira_disease_hospitalization_outpatient_stats", timeout)
    monkeypatch.setattr(external, "web_search", web_search)
    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "ENFORCE")
    monkeypatch.setenv("CHAT_TOOL_ROUTING_OFFICIAL_WEB_FALLBACK_ENABLED", "true")

    payload = run_external_tool_agent(
        "HIRA: 상병코드 D693의 최근 5개년 환자수 추이를 분석해줘",
        resolver=BrandResolver(),
        external=external,
        provider=_no_tool_provider(),
    )

    assert web_calls == 1
    assert [call["tool"] for call in payload["tool_calls"]].count("web_search") == 1
    assert "요청한 HIRA" in payload["answer"]
    assert "대신한 값이 아니라" in payload["answer"]
    assert payload["router_diagnostics"]["routing_v4"]["executed_call_signature"][
        "reason_code"
    ] == "UPSTREAM_UNAVAILABLE"


def test_d06c_internal_only_request_never_executes_web_fallback(monkeypatch) -> None:
    external = ExternalApiClient(mode="fixture")
    web_calls = 0

    def timeout(sick_cd: str, *, year: str = "2024") -> ExternalCall:
        del sick_cd, year
        raise requests.Timeout("authoritative timeout")

    def web_search(
        query: str,
        max_results: int = 5,
        *,
        topic: str = "general",
    ) -> ExternalCall:
        nonlocal web_calls
        del query, max_results, topic
        web_calls += 1
        raise AssertionError("internal-only request must not execute web fallback")

    monkeypatch.setattr(external, "hira_disease_hospitalization_outpatient_stats", timeout)
    monkeypatch.setattr(external, "web_search", web_search)
    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "ENFORCE")
    monkeypatch.setenv("CHAT_TOOL_ROUTING_OFFICIAL_WEB_FALLBACK_ENABLED", "true")

    payload = run_external_tool_agent(
        "상병코드 D693 환자수는 내부 데이터만 사용해서 알려줘",
        resolver=BrandResolver(),
        external=external,
        provider=_no_tool_provider(),
    )

    fallback = payload["router_diagnostics"]["routing_v4"]["official_web_fallback"]
    assert web_calls == 0
    assert fallback["eligible"] is False
    assert fallback["reason_code"] == "INTERNAL_ONLY"


def test_d06c_no_record_found_uses_one_official_web_supplement(monkeypatch) -> None:
    external = ExternalApiClient(mode="fixture")
    web_calls = 0

    def no_rows(sick_cd: str, *, year: str = "2024") -> ExternalCall:
        return ExternalCall(
            tool="hira_disease_hospitalization_outpatient_stats",
            source="external_api",
            status="no_data",
            summary_text=f"{sick_cd} {year}: no rows",
            render_data={
                "error_code": "NO_DATA",
                "items": [],
                "request": {"sick_cd": sick_cd, "year": year},
            },
        )

    def web_search(
        query: str,
        max_results: int = 5,
        *,
        topic: str = "general",
    ) -> ExternalCall:
        nonlocal web_calls
        del query, max_results, topic
        web_calls += 1
        return ExternalCall(
            tool="web_search",
            source="web_search",
            status="ok",
            summary_text="official supplement",
            render_data={
                "provider": "fixture",
                "items": [
                    {
                        "title": "HIRA official search",
                        "url": "https://opendata.hira.or.kr/search",
                    }
                ],
            },
        )

    monkeypatch.setattr(external, "hira_disease_hospitalization_outpatient_stats", no_rows)
    monkeypatch.setattr(external, "web_search", web_search)
    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "ENFORCE")
    monkeypatch.setenv("CHAT_TOOL_ROUTING_OFFICIAL_WEB_FALLBACK_ENABLED", "true")

    payload = run_external_tool_agent(
        "상병코드 D693의 최근 5개년 환자수 추이를 분석해줘",
        resolver=BrandResolver(),
        external=external,
        provider=_no_tool_provider(),
    )

    fallback = payload["router_diagnostics"]["routing_v4"]["official_web_fallback"]
    assert web_calls == 1
    assert fallback["reason_code"] == "NO_RECORD_FOUND"
    assert fallback["calls_executed"] == 1
    assert "공식 웹 보완 자료" in payload["answer"]


def test_d06b_official_web_fallback_accepts_only_allowlisted_sources_when_enabled(
    monkeypatch,
) -> None:
    monkeypatch.setenv("CHAT_TOOL_ROUTING_OFFICIAL_WEB_FALLBACK_ENABLED", "true")

    decision = routing_v4_execution.official_web_fallback_policy(
        source_domain="hira",
        runtime_reason="UPSTREAM_UNAVAILABLE",
        usable_authoritative_results=0,
        candidate_urls=(
            "https://opendata.hira.or.kr/op/opc/olapHthInsRvStatInfoTab1.do",
            "https://blog.naver.com/unverified-statistics",
            "https://hira.or.kr.evil.example/spoofed",
            "https://www.hira.or.kr/bbsDummy.do",
        ),
    )

    assert decision.web_call_budget == 1
    assert decision.accepted_urls == (
        "https://opendata.hira.or.kr/op/opc/olapHthInsRvStatInfoTab1.do",
        "https://www.hira.or.kr/bbsDummy.do",
    )
    assert decision.separate_section is True
    assert "UPSTREAM_UNAVAILABLE" not in decision.disclosure
    assert "공식 웹 보완 자료" in decision.disclosure


@pytest.mark.parametrize(
    "candidate_urls",
    (
        (),
        ("https://blog.naver.com/unverified-statistics",),
        ("https://hira.or.kr.evil.example/spoofed",),
    ),
)
def test_d06b_web_fallback_budget_stays_closed_without_an_official_url(
    monkeypatch,
    candidate_urls: tuple[str, ...],
) -> None:
    monkeypatch.setenv("CHAT_TOOL_ROUTING_OFFICIAL_WEB_FALLBACK_ENABLED", "true")

    decision = routing_v4_execution.official_web_fallback_policy(
        source_domain="hira",
        runtime_reason="UPSTREAM_UNAVAILABLE",
        usable_authoritative_results=0,
        candidate_urls=candidate_urls,
    )

    assert decision.web_call_budget == 0
    assert decision.accepted_urls == ()
    assert decision.separate_section is False
    assert decision.disclosure == ""


def test_d06b_partial_authoritative_result_never_enables_web_fallback(monkeypatch) -> None:
    monkeypatch.setenv("CHAT_TOOL_ROUTING_OFFICIAL_WEB_FALLBACK_ENABLED", "true")

    decision = routing_v4_execution.official_web_fallback_policy(
        source_domain="hira",
        runtime_reason="UPSTREAM_UNAVAILABLE",
        usable_authoritative_results=1,
        candidate_urls=("https://opendata.hira.or.kr/official",),
    )

    assert decision.web_call_budget == 0
    assert decision.accepted_urls == ()
    assert decision.separate_section is False
    assert decision.reason_code == "PARTIAL_RESULT"


def test_d06b_web_fallback_flag_is_off_by_default(monkeypatch) -> None:
    monkeypatch.delenv("CHAT_TOOL_ROUTING_OFFICIAL_WEB_FALLBACK_ENABLED", raising=False)

    decision = routing_v4_execution.official_web_fallback_policy(
        source_domain="hira",
        runtime_reason="UPSTREAM_UNAVAILABLE",
        usable_authoritative_results=0,
        candidate_urls=("https://opendata.hira.or.kr/official",),
    )

    assert decision.web_call_budget == 0
    assert decision.accepted_urls == ()
    assert decision.reason_code == "UPSTREAM_UNAVAILABLE"


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
