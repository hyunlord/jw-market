from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from typing import Any, Final

from jw_chat_agent_poc.common.timing import Timing
from jw_chat_agent_poc.orchestrator.answer_contract import CONTRACT_REQUIRED_TOOLS, evaluate_answer_contract
from jw_chat_agent_poc.orchestrator.hira_disease import hira_disease_code_for_text
from jw_chat_agent_poc.orchestrator.tool_use_contract import tool_use_requirements
from jw_chat_agent_poc.orchestrator.tool_use_contract import tool_use_evidence_complete
from jw_chat_agent_poc.resolver import BrandResolver, UnsupportedBrandError
from jw_chat_agent_poc.tool_use.contracts import AgentResult
from jw_chat_agent_poc.tool_use.executor import AgentExecutor
from jw_chat_agent_poc.tool_use.ledger import EvidenceLedger
from jw_chat_agent_poc.tool_use.provider import GenosToolChoiceProvider, ToolChoice, ToolChoiceProvider
from jw_chat_agent_poc.tool_use.registry import ExternalToolRegistry
from jw_chat_agent_poc.tool_use.routing_v4_runtime import (
    begin_shadow_route_diagnostics,
    complete_shadow_route_diagnostics,
    configured_routing_mode,
    execute_enforced_route,
    internal_legacy_route_diagnostics,
    shadow_route_diagnostics,
)
from jw_chat_agent_poc.tool_use.routing_v4_rules import classify_question
from jw_chat_agent_poc.tool_use.routing_v4_types import RoutingMode
from jw_chat_agent_poc.tool_use.specs import ToolSpec
from jw_chat_agent_poc.tools.external import ExternalApiClient


LOGGER = logging.getLogger(__name__)
FEATURE_FLAG: Final[str] = "CHAT_EXTERNAL_TOOL_AGENT_ENABLED"
FORCE_CONTRACT_CALLS_FLAG: Final[str] = "CHAT_EXTERNAL_TOOL_FORCE_CONTRACT_CALLS"
_INTERNAL_MART_TOOL_NAMES: Final[frozenset[str]] = frozenset(
    {
        "agent_calculation",
        "bq_analysis",
        "csd_activity_trend",
        "general_view_dynamic_market",
        "get_brand_metric",
        "get_market_landscape",
        "market_scope",
        "query_spec",
    }
)
_CLINICAL_DISEASE_ALIASES: Final[dict[str, str]] = {
    "고지혈증": "hyperlipidemia",
    "뇌경색": "cerebral infarction",
}


def external_tool_agent_enabled() -> bool:
    return os.environ.get(FEATURE_FLAG, "1").lower() in {"1", "true", "yes"}


def run_external_tool_agent(
    question: str,
    *,
    resolver: BrandResolver,
    external: ExternalApiClient,
    provider: ToolChoiceProvider | None = None,
    routing_provider: ToolChoiceProvider | None = None,
    timing: Timing | None = None,
) -> dict[str, Any]:
    mode = configured_routing_mode()
    if mode is RoutingMode.OFF:
        return _run_legacy_external_tool_agent(
            question,
            resolver=resolver,
            external=external,
            provider=provider,
            timing=timing,
        )

    registry = ExternalToolRegistry(resolver=resolver, external=external)
    tools = registry.list_for_query(question)
    if mode is RoutingMode.SHADOW:
        selected_routing_provider = routing_provider or GenosToolChoiceProvider.from_env()
        shadow_task = begin_shadow_route_diagnostics(
            question,
            tools=tools,
            provider=selected_routing_provider,
        )
        payload = _run_legacy_external_tool_agent(
            question,
            resolver=resolver,
            external=external,
            provider=provider,
            timing=timing,
        )
        diagnostics = complete_shadow_route_diagnostics(shadow_task)
        return _attach_routing_v4_diagnostics(payload, diagnostics)

    selected_routing_provider = routing_provider or provider or GenosToolChoiceProvider.from_env()
    enforced = execute_enforced_route(
        question,
        tools=tools,
        provider=selected_routing_provider,
        completion_policy=_external_evidence_complete,
        timing=timing,
    )
    payload = _agent_result_payload(question, enforced.result, timing=timing)
    payload["router_diagnostics"]["routing_v4"] = enforced.diagnostics
    return payload


def attach_routing_v4_legacy_observation(
    question: str,
    payload: dict[str, Any],
    *,
    resolver: BrandResolver | None = None,
    external: ExternalApiClient | None = None,
    routing_provider: ToolChoiceProvider | None = None,
) -> dict[str, Any]:
    """Attach v4 observations to legacy paths without changing their response."""

    mode = configured_routing_mode()
    diagnostics = payload.get("router_diagnostics")
    if mode is RoutingMode.OFF or (
        isinstance(diagnostics, dict) and isinstance(diagnostics.get("routing_v4"), dict)
    ):
        return payload

    tool_calls = payload.get("tool_calls")
    if _has_internal_mart_call(tool_calls):
        metrics = payload.get("agent_loop_metrics")
        runtime_status = (
            str(metrics.get("status") or "legacy")
            if isinstance(metrics, dict)
            else "legacy"
        )
        observation = internal_legacy_route_diagnostics(mode, runtime_status=runtime_status)
        return _attach_routing_v4_diagnostics(payload, observation)

    classification = classify_question(question)
    if (
        mode is RoutingMode.SHADOW
        and classification.source_domain != "unresolved"
        and resolver is not None
        and external is not None
    ):
        registry = ExternalToolRegistry(resolver=resolver, external=external)
        provider = routing_provider or GenosToolChoiceProvider.from_env()
        observation = shadow_route_diagnostics(
            question,
            tools=registry.list_for_query(question),
            provider=provider,
        )
        return _attach_routing_v4_diagnostics(payload, observation)
    return payload


def _has_internal_mart_call(tool_calls: object) -> bool:
    if not isinstance(tool_calls, list):
        return False
    return any(
        isinstance(call, dict)
        and str(call.get("tool") or "") in _INTERNAL_MART_TOOL_NAMES
        for call in tool_calls
    )


def _attach_routing_v4_diagnostics(
    payload: dict[str, Any],
    diagnostics: dict[str, Any],
) -> dict[str, Any]:
    before_sha256 = _legacy_response_sha256(payload)
    annotated = dict(payload)
    existing = annotated.get("router_diagnostics")
    router_diagnostics = dict(existing) if isinstance(existing, dict) else {}
    routing_v4 = dict(diagnostics)
    router_diagnostics["routing_v4"] = routing_v4
    annotated["router_diagnostics"] = router_diagnostics
    after_sha256 = _legacy_response_sha256(annotated)
    routing_v4["legacy_response_invariant"] = {
        "before_sha256": before_sha256,
        "after_sha256": after_sha256,
        "unchanged": before_sha256 == after_sha256,
    }
    return annotated


def _legacy_response_sha256(payload: dict[str, Any]) -> str:
    visible_fields = (
        "question",
        "resolution",
        "decomposition",
        "tool_calls",
        "answer",
        "markdown_response",
        "sources",
    )
    visible = {key: payload.get(key) for key in visible_fields if key in payload}
    canonical = json.dumps(
        visible,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _run_legacy_external_tool_agent(
    question: str,
    *,
    resolver: BrandResolver,
    external: ExternalApiClient,
    provider: ToolChoiceProvider | None,
    timing: Timing | None,
) -> dict[str, Any]:
    registry = ExternalToolRegistry(resolver=resolver, external=external)
    selected_provider = provider or GenosToolChoiceProvider.from_env()
    force_contract_calls = os.environ.get(FORCE_CONTRACT_CALLS_FLAG, "0").lower() in {"1", "true", "yes"}
    forced_choices = (
        _deterministic_tool_choices(question, resolver)
        if provider is None and force_contract_calls
        else ()
    )
    result = AgentExecutor(
        provider=selected_provider,
        completion_policy=_external_evidence_complete,
        best_effort=True,
        forced_choices=forced_choices,
        parallel_forced_choices=bool(forced_choices),
        timing=timing,
    ).run(user_text=question, tools=registry.list_for_query(question))
    if result.fallback_code is not None:
        LOGGER.info("external tool agent fallback code=%s", result.fallback_code.value)
    return _agent_result_payload(question, result, timing=timing)


def _deterministic_tool_choices(question: str, resolver: BrandResolver) -> tuple[ToolChoice, ...]:
    """Turn explicit evidence contracts into calls before consulting the planner."""

    try:
        resolution = resolver.resolve(question, allow_default=False)
    except UnsupportedBrandError:
        resolution = None
    brand = resolution.canonical_brand if resolution is not None else _explicit_brand_query(question)
    ingredient = (
        resolution.molecule_en[0]
        if resolution is not None and resolution.molecule_en
        else None
    )
    disease_query = _disease_query(question)

    contract = evaluate_answer_contract(question, "", None)
    contract_key = str(contract.get("structural_contract") or contract.get("intent") or "")
    lowered = question.casefold()
    combined_clinical_review = (
        contract_key == "clinical_evidence"
        and any(token in lowered for token in ("임상", "clinical"))
        and any(token in lowered for token in ("허가", "permission", "approval"))
    )
    requested = list(CONTRACT_REQUIRED_TOOLS.get(contract_key, ())) if combined_clinical_review else []
    for requirement in tool_use_requirements(question):
        preferred = _preferred_requirement_tool(requirement.alternatives)
        if preferred is not None and preferred not in requested:
            requested.append(preferred)
    if combined_clinical_review and resolution is None and "web_search" not in requested:
        requested.append("web_search")

    choices: list[ToolChoice] = []
    for tool_name in requested:
        arguments = _deterministic_arguments(tool_name, question, brand, ingredient, disease_query)
        if arguments is None:
            continue
        choices.append(
            ToolChoice(
                tool_name,
                arguments,
                f"contract requires {tool_name}",
                call_id=f"contract-{len(choices) + 1}",
            )
        )
    return tuple(choices)


def _preferred_requirement_tool(alternatives: frozenset[str]) -> str | None:
    preference = (
        "local_molecule_lookup",
        "clinicaltrials_v2_search",
        "mfds_clinical_trial_kr",
        "mfds_permission_search",
        "openfda_label_search",
        "mfds_patent",
        "mfds_fda_orangebook",
        "web_search",
        "hira_disease_name_code",
        "hira_disease_hospitalization_outpatient_stats",
        "hira_disease_gender_age_stats",
        "hira_disease_institution_class_stats",
        "hira_disease_area_stats",
    )
    return next((name for name in preference if name in alternatives), None)


def _deterministic_arguments(
    tool_name: str,
    question: str,
    brand: str | None,
    ingredient: str | None,
    disease_query: str | None,
) -> dict[str, Any] | None:
    if tool_name in {"local_molecule_lookup", "get_drug_main_ingredient", "mfds_permission_search"}:
        return {"brand": brand} if brand is not None else None
    if tool_name in {"clinicaltrials_v2_search", "mfds_clinical_trial_kr"}:
        if ingredient is not None:
            return {"query": ingredient}
        if disease_query is None:
            return None
        query = (
            _CLINICAL_DISEASE_ALIASES.get(disease_query, disease_query)
            if tool_name == "clinicaltrials_v2_search"
            else disease_query
        )
        return {"query": query, "query_type": "condition"}
    if tool_name == "openfda_label_search":
        if ingredient is None:
            return None
        evidence_type = "adverse_event" if any(token in question.casefold() for token in ("부작용", "이상반응", "adverse")) else "label"
        return {"ingredient": ingredient, "evidence_type": evidence_type}
    if tool_name in {"mfds_patent", "mfds_fda_orangebook"}:
        return {"ingredient": ingredient} if ingredient is not None else None
    if tool_name == "web_search":
        return {"query": question, "brand": brand, "topic": "general"}
    if tool_name.startswith("hira_disease_"):
        sick_cd = hira_disease_code_for_text(brand or question)
        return {"sick_cd": sick_cd, "year": "2024"} if sick_cd is not None else None
    return None


def _disease_query(question: str) -> str | None:
    """Extract a disease phrase without forwarding the full user sentence as a drug name."""

    tokens = re.findall(r"[가-힣A-Za-z0-9]+", question)
    suffixes = ("증", "병", "암", "염", "장애")
    suffixed = next((token for token in tokens if len(token) >= 2 and token.endswith(suffixes)), None)
    if suffixed is not None:
        return suffixed
    clinical_subject = re.search(
        r"(?P<subject>[가-힣A-Za-z0-9]{2,40})\s+(?:질환\s*)?(?:임상|clinical)\b",
        question,
        flags=re.IGNORECASE,
    )
    return clinical_subject.group("subject") if clinical_subject is not None else None


def _explicit_brand_query(question: str) -> str | None:
    """Keep short brand-only requests usable without treating full sentences as brands."""

    match = re.fullmatch(r"\s*([가-힣A-Za-z0-9+_-]{2,40})\s+(?:성분|주성분)\s*[?.!。？！]*\s*", question)
    return match.group(1) if match is not None else None


def _external_evidence_complete(
    *,
    user_text: str,
    ledger: EvidenceLedger,
    spec: ToolSpec | None,
    tool_calls: tuple[dict, ...],
) -> bool:
    del spec
    return ledger.is_complete() and tool_use_evidence_complete(user_text, tool_calls)


def _agent_result_payload(
    question: str,
    result: AgentResult,
    *,
    timing: Timing | None = None,
) -> dict[str, Any]:
    verified_statuses = {"ok", "partial"}
    payload = {
        "question": question,
        "resolution": None,
        "decomposition": [{"intent": "external_tool_agent", "status": result.status}],
        "router_diagnostics": {"mode": "tool_use_agent", "fallback_code": result.fallback_code.value if result.fallback_code else None},
        "tool_calls": list(result.tool_calls),
        "answer": result.answer,
        "markdown_response": {
            "markdown": result.answer,
            "fact_md": result.answer if result.status in verified_statuses else "",
            "data_md": "",
            "verification": {
                "status": (
                    "pass"
                    if result.status == "ok"
                    else "partial"
                    if result.status == "partial"
                    else "fail"
                )
            },
        },
        "sources": list(result.sources),
        "agent_trace": [trace.model_dump() for trace in result.traces],
        "agent_loop_metrics": {
            "status": result.status,
            "tool_calls": len(result.tool_calls),
            "fallback_code": result.fallback_code.value if result.fallback_code else None,
        },
    }
    if timing is not None:
        payload["timing"] = timing
    return payload
