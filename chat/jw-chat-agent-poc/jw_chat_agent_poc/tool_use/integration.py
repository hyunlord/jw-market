from __future__ import annotations

import logging
import os
from typing import Any, Final

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
from jw_chat_agent_poc.tool_use.specs import ToolSpec
from jw_chat_agent_poc.tools.external import ExternalApiClient


LOGGER = logging.getLogger(__name__)
FEATURE_FLAG: Final[str] = "CHAT_EXTERNAL_TOOL_AGENT_ENABLED"
FORCE_CONTRACT_CALLS_FLAG: Final[str] = "CHAT_EXTERNAL_TOOL_FORCE_CONTRACT_CALLS"
def external_tool_agent_enabled() -> bool:
    return os.environ.get(FEATURE_FLAG, "1").lower() in {"1", "true", "yes"}


def run_external_tool_agent(
    question: str,
    *,
    resolver: BrandResolver,
    external: ExternalApiClient,
    provider: ToolChoiceProvider | None = None,
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
    ).run(user_text=question, tools=registry.list_for_query(question))
    if result.fallback_code is not None:
        LOGGER.info("external tool agent fallback code=%s", result.fallback_code.value)
    return _agent_result_payload(question, result)


def _deterministic_tool_choices(question: str, resolver: BrandResolver) -> tuple[ToolChoice, ...]:
    """Turn explicit evidence contracts into calls before consulting the planner."""

    try:
        resolution = resolver.resolve(question, allow_default=False)
    except UnsupportedBrandError:
        resolution = None
    brand = resolution.canonical_brand if resolution is not None else question.strip()
    ingredient = (
        resolution.molecule_en[0]
        if resolution is not None and resolution.molecule_en
        else question.strip()
    )

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

    choices: list[ToolChoice] = []
    for tool_name in requested:
        arguments = _deterministic_arguments(tool_name, question, brand, ingredient)
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
    )
    return next((name for name in preference if name in alternatives), None)


def _deterministic_arguments(
    tool_name: str,
    question: str,
    brand: str,
    ingredient: str,
) -> dict[str, Any] | None:
    if tool_name in {"local_molecule_lookup", "get_drug_main_ingredient", "mfds_permission_search"}:
        return {"brand": brand}
    if tool_name in {"clinicaltrials_v2_search", "mfds_clinical_trial_kr"}:
        return {"query": ingredient if ingredient != question.strip() else question.strip()}
    if tool_name == "openfda_label_search":
        evidence_type = "adverse_event" if any(token in question.casefold() for token in ("부작용", "이상반응", "adverse")) else "label"
        return {"ingredient": ingredient, "evidence_type": evidence_type}
    if tool_name in {"mfds_patent", "mfds_fda_orangebook"}:
        return {"ingredient": ingredient}
    if tool_name == "web_search":
        return {"query": question, "brand": brand, "topic": "general"}
    if tool_name.startswith("hira_disease_"):
        return {"sick_cd": hira_disease_code_for_text(question) or question, "year": "2024"}
    return None


def _external_evidence_complete(
    *,
    user_text: str,
    ledger: EvidenceLedger,
    spec: ToolSpec | None,
    tool_calls: tuple[dict, ...],
) -> bool:
    del spec
    return ledger.is_complete() and tool_use_evidence_complete(user_text, tool_calls)


def _agent_result_payload(question: str, result: AgentResult) -> dict[str, Any]:
    return {
        "question": question,
        "resolution": None,
        "decomposition": [{"intent": "external_tool_agent", "status": result.status}],
        "router_diagnostics": {"mode": "tool_use_agent", "fallback_code": result.fallback_code.value if result.fallback_code else None},
        "tool_calls": list(result.tool_calls),
        "answer": result.answer,
        "markdown_response": {
            "markdown": result.answer,
            "fact_md": result.answer if result.status == "ok" else "",
            "data_md": "",
            "verification": {"status": "pass" if result.status == "ok" else "fail"},
        },
        "sources": list(result.sources),
        "agent_trace": [trace.model_dump() for trace in result.traces],
        "agent_loop_metrics": {
            "status": result.status,
            "tool_calls": len(result.tool_calls),
            "fallback_code": result.fallback_code.value if result.fallback_code else None,
        },
    }
