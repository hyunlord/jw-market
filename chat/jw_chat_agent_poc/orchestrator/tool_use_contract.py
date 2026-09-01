from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Mapping, Sequence

from jw_chat_agent_poc.orchestrator.hira_disease import (
    HIRA_TREND_YEARS,
    hira_stat_requests,
    is_hira_disease_question,
)


_FAILED_STATUSES: Final[frozenset[str]] = frozenset(
    {
        "error",
        "query_failed",
        "mapping_failed",
        "timeout",
        "failed",
        "no_data",
        "unsupported",
        "missing",
        "not_found",
        "incomplete_split",
    }
)
_GROUNDING_ONLY_TOOLS: Final[frozenset[str]] = frozenset({"hira_disease_name_code"})


@dataclass(frozen=True, slots=True)
class ToolUseRequirement:
    label: str
    alternatives: frozenset[str]
    minimum_calls: int = 1
    required_periods: frozenset[str] = frozenset()
    required_evidence_metrics: frozenset[str] = frozenset()


def tool_use_requirements(question: str) -> tuple[ToolUseRequirement, ...]:
    """Return verification requirements without selecting or routing a tool."""

    from jw_chat_agent_poc.tool_use.routing_v4_rules import classify_question

    lowered = question.casefold()
    classification = classify_question(question)
    requirements: list[ToolUseRequirement] = []
    if is_hira_disease_question(question):
        requirements.extend(_hira_requirements(lowered))
    if any(token in lowered for token in ("허가", "permission", "approval")):
        requirements.append(_one("허가 정보", "mfds_permission_search"))
    if any(token in lowered for token in ("부작용", "이상반응", "adverse", "side effect")):
        requirements.append(
            ToolUseRequirement(
                label="FDA 이상반응",
                alternatives=frozenset({"openfda_label_search"}),
                required_evidence_metrics=frozenset({"FAERS 자발보고 내 이상반응"}),
            )
        )
    elif any(token in lowered for token in ("안전성", "safety", "fda 라벨")):
        requirements.append(_one("FDA 라벨/이상반응", "openfda_label_search"))
    if any(token in lowered for token in ("오렌지북", "orange book", "orangebook")):
        requirements.append(_one("FDA Orange Book", "mfds_fda_orangebook"))
    elif any(token in lowered for token in ("특허", "독점권", "만료", "patent")):
        requirements.append(_one("특허/독점권", "mfds_patent", "mfds_fda_orangebook"))
    if classification.requested_capability == "CLINICAL_TRIAL_NCT_DETAIL_FIELDS":
        requirements.append(_one("글로벌 임상시험 상세", "clinicaltrials_study_details"))
    elif any(token in lowered for token in ("국내 임상", "한국 임상", "식약처 임상", "mfds 임상")):
        requirements.append(_one("국내 임상시험", "mfds_clinical_trial_kr"))
    elif any(token in lowered for token in ("임상", "clinical", "nct")):
        requirements.append(_one("글로벌 임상시험", "clinicaltrials_v2_search"))
    if any(token in lowered for token in ("가이드라인", "치료 지침", "guideline")):
        requirements.append(_one("웹 검색", "web_search"))
    if classification.requested_capability == "MFDS_COMPOSITION":
        requirements.append(_one("성분 조성", "mfds_composition"))
    elif any(token in lowered for token in ("성분", "주성분", "molecule", "ingredient")):
        requirements.append(_one("성분", "local_molecule_lookup", "get_drug_main_ingredient"))
    return tuple(requirements)


def tool_use_evidence_complete(question: str, calls: Sequence[Mapping[str, Any]]) -> bool:
    """Return whether successful evidence satisfies every question requirement."""

    requirements = tool_use_requirements(question)
    if requirements:
        return not missing_tool_use_requirements(question, calls)
    return any(
        _successful_evidence_call(call) and _tool_name(call) not in _GROUNDING_ONLY_TOOLS
        for call in calls
    )


def missing_tool_use_requirements(
    question: str,
    calls: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    """List unmet evidence requirements for a connected Tool-Use answer."""

    missing: list[str] = []
    for requirement in tool_use_requirements(question):
        matching = tuple(
            call
            for call in calls
            if _tool_name(call) in requirement.alternatives and _successful_evidence_call(call)
        )
        if len(matching) < requirement.minimum_calls:
            missing.append(requirement.label)
            continue
        periods = frozenset(period for call in matching for period in _call_periods(call))
        if not requirement.required_periods.issubset(periods):
            missing.append(requirement.label)
            continue
        metrics = frozenset(metric for call in matching for metric in _call_evidence_metrics(call))
        if not requirement.required_evidence_metrics.issubset(metrics):
            missing.append(requirement.label)
    return tuple(missing)


def tool_call_status(call: Mapping[str, Any]) -> str:
    """Prefer an explicit envelope status, then preserve the top-level status."""

    render_data = call.get("render_data")
    if isinstance(render_data, Mapping) and "status" in render_data:
        return str(render_data.get("status") or "error").casefold()
    return str(call.get("status") or "error").casefold()


def _hira_requirements(lowered: str) -> tuple[ToolUseRequirement, ...]:
    """Publish, as requirements, the statistics the executor was told to call.

    The vocabulary lives in hira_disease.hira_stat_requests so that the layer
    doing the calling and the layer checking the calls cannot disagree. They used
    to, on the default: this function asked for one statistic while the executor
    called four, and the three extra were banded tables nothing had requested.
    """
    requests = hira_stat_requests(lowered)
    if not requests:
        return (_one("HIRA 질병명", "hira_disease_name_code"),)
    return tuple(
        ToolUseRequirement(
            label=request.label,
            alternatives=frozenset({request.tool}),
            minimum_calls=len(request.periods) or 1,
            required_periods=frozenset(request.periods),
        )
        for request in requests
    )


def _one(label: str, *tools: str) -> ToolUseRequirement:
    return ToolUseRequirement(label=label, alternatives=frozenset(tools))


def _successful_evidence_call(call: Mapping[str, Any]) -> bool:
    if tool_call_status(call) in _FAILED_STATUSES:
        return False
    render_data = call.get("render_data")
    if not isinstance(render_data, Mapping) or render_data.get("ok") is False:
        return False
    evidence = render_data.get("evidence")
    return isinstance(evidence, Sequence) and not isinstance(evidence, (str, bytes)) and bool(evidence)


def _tool_name(call: Mapping[str, Any]) -> str:
    return str(call.get("tool") or "")


def _call_periods(call: Mapping[str, Any]) -> frozenset[str]:
    render_data = call.get("render_data")
    if not isinstance(render_data, Mapping):
        return frozenset()
    periods: set[str] = set()
    evidence = render_data.get("evidence")
    if isinstance(evidence, Sequence) and not isinstance(evidence, (str, bytes)):
        for fact in evidence:
            if isinstance(fact, Mapping) and fact.get("period") not in (None, ""):
                periods.add(str(fact["period"]))
    request = render_data.get("request")
    if isinstance(request, Mapping) and request.get("year") not in (None, ""):
        periods.add(str(request["year"]))
    return frozenset(periods)


def _call_evidence_metrics(call: Mapping[str, Any]) -> frozenset[str]:
    render_data = call.get("render_data")
    if not isinstance(render_data, Mapping):
        return frozenset()
    evidence = render_data.get("evidence")
    if not isinstance(evidence, Sequence) or isinstance(evidence, (str, bytes)):
        return frozenset()
    return frozenset(
        str(fact["metric"])
        for fact in evidence
        if isinstance(fact, Mapping) and fact.get("metric") not in (None, "")
    )
