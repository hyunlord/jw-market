from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from jw_chat_agent_poc.agent_loop.element_ledger import request_metrics
from jw_chat_agent_poc.agent_loop.requested_source import (
    extract_requested_sources,
    served_source_from_calls,
)
from jw_chat_agent_poc.common.periods import requested_period
from jw_chat_agent_poc.orchestrator.provenance import EvidenceFact
from jw_chat_agent_poc.orchestrator.provenance_model import MISSING_LABEL, public_view
from jw_chat_agent_poc.orchestrator.source_trap import requested_unavailable_source
from jw_chat_agent_poc.service.conversation import ConversationSlots
from jw_chat_agent_poc.service.evidence_binding import evidence_facts_from_result
from jw_chat_agent_poc.tools.metrics.market_scope_intent import (
    _explicit_view,
    _normalize,
    detect_market_scope_intent,
)


Presence = Literal["explicit", "inherited", "unset"]
Comparison = Literal["match", "mismatch", "unverifiable", "not_applicable"]

_TIER_A: Final[tuple[str, ...]] = ("entity", "source", "metric", "view", "period")
_NON_COMPARABLE: Final[tuple[str, ...]] = ("granularity", "relation", "scope")
_INHERITED_RECOGNISERS: Final[dict[str, frozenset[str]]] = {
    "entity": frozenset(
        {
            "brand_pronoun",
            "bare_brand_switch",
            "contrast",
            "implicit_brand",
            "generic",
            "first_rank",
            "anchor_brand",
        }
    ),
    "metric": frozenset({"bare_metric"}),
    "view": frozenset({"bare_market"}),
    "period": frozenset({"bare_period", "relative_period", "same_period"}),
}
_METRIC_ALLOW: Final[frozenset[str]] = frozenset(
    {
        "activity",
        "competition",
        "concentration",
        "cr5",
        "growth",
        "hhi",
        "market_share",
        "market_size",
        "momentum",
        "news",
        "patient_count",
        "prescription",
        "prescription_count",
        "prescription_dispensing_amount",
        "prescription_volume",
        "rank",
        "sales",
        "series",
        "share",
        "threat",
    }
)
_SOURCE_ALLOW: Final[frozenset[str]] = frozenset(
    {"ubist", "iqvia_nsa", "cortellis", "datamonitor", "kol", "nccn"}
)
_VIEW_ALLOW: Final[frozenset[str]] = frozenset(
    {"general_view", "market_landscape", "competitive_dynamics"}
)
_METRIC_ANSWER_TERMS: Final[dict[str, tuple[str, ...]]] = {
    "sales": ("매출", "판매"),
    "hhi": ("HHI",),
    "cr5": ("CR5",),
    "concentration": ("집중도",),
    "market_share": ("점유율",),
    "share": ("점유율",),
    "market_size": ("시장 규모", "시장규모"),
    "patient_count": ("환자수", "환자 수"),
    "prescription_volume": ("처방량", "처방 량"),
    "rank": ("순위",),
}
_VIEW_ANSWER_TERMS: Final[dict[str, tuple[str, ...]]] = {
    "general_view": ("일반뷰", "ATC4"),
    "market_landscape": ("market_landscape", "전략뷰"),
    "competitive_dynamics": ("competitive_dynamics", "경쟁시장"),
}
_SOURCE_ANSWER_TERMS: Final[dict[str, tuple[str, ...]]] = {
    "ubist": ("UBIST", "유비스트"),
    "iqvia_nsa": ("IQVIA", "아이큐비아"),
}


@dataclass(frozen=True, slots=True)
class _SlotInput:
    value: str | None
    presence: Presence
    extractor: str | None
    status: str


def slot_observability(
    *,
    question: str,
    result: Mapping[str, Any],
    conversation_slots: ConversationSlots,
    answer: str,
) -> dict[str, dict[str, Any]]:
    """Describe requested and served slots without affecting answer decisions."""

    calls = _calls(result)
    facts = evidence_facts_from_result(result)
    requested = _requested_slots(question, result)
    served = _served_slots(calls, facts, conversation_slots)

    slots: dict[str, dict[str, Any]] = {}
    for slot in _TIER_A:
        request = requested[slot]
        served_value = served[slot]
        slots[slot] = {
            "presence": request.presence,
            "origin_turn": None,
            "extractor": request.extractor,
            "status": request.status,
            "requested_present": request.value is not None,
            "served_present": served_value is not None,
            "requested_value": _public_requested_value(slot, request.value),
            "comparison": _comparison(request.value, served_value),
            "present_in_answer": _present_in_answer(
                slot,
                served_value,
                answer,
            ),
        }

    for slot in _NON_COMPARABLE:
        request = requested[slot]
        slots[slot] = {
            "presence": request.presence,
            "origin_turn": None,
            "extractor": request.extractor,
            "status": request.status,
            "requested_present": request.value is not None,
            "served_present": False,
            "requested_value": None,
            "comparison": "not_applicable",
            "present_in_answer": None,
        }
    return slots


def _requested_slots(
    question: str,
    result: Mapping[str, Any],
) -> dict[str, _SlotInput]:
    anaphora = result.get("_qa_anaphora")
    observation = anaphora if isinstance(anaphora, Mapping) else {}
    recogniser = str(observation.get("recogniser") or "")
    resolved = observation.get("status") == "resolved"

    resolution = result.get("resolution")
    entity = (
        str(resolution.get("canonical_brand") or "").strip()
        if isinstance(resolution, Mapping)
        else ""
    )
    sources = extract_requested_sources(question)
    unavailable_source = requested_unavailable_source(question)
    source_candidates = (
        sources
        if sources
        else ((unavailable_source.key,) if unavailable_source is not None else ())
    )
    source = (
        source_candidates[0]
        if len(source_candidates) == 1 and source_candidates[0] in _SOURCE_ALLOW
        else None
    )
    metric = _explicit_metric(question)
    view = _explicit_view(_normalize(question))
    period = requested_period(question)

    requested = {
        "entity": _request_slot(
            entity or None,
            "resolution",
            inherited=resolved and recogniser in _INHERITED_RECOGNISERS["entity"],
        ),
        "source": _request_slot(source, "requested_source"),
        "metric": _request_slot(
            metric,
            "explicit_metric",
            inherited=resolved and recogniser in _INHERITED_RECOGNISERS["metric"],
        ),
        "view": _request_slot(
            view,
            "explicit_view",
            inherited=(
                view is None
                and resolved
                and recogniser in _INHERITED_RECOGNISERS["view"]
            ),
            default_suppressed=(
                view is None and detect_market_scope_intent(question) is not None
            ),
        ),
        "period": _request_slot(
            period,
            "requested_period",
            inherited=resolved and recogniser in _INHERITED_RECOGNISERS["period"],
        ),
    }

    relation = "competition" if "competition" in request_metrics(question) else None
    scope = "market" if detect_market_scope_intent(question) is not None else None
    requested.update(
        {
            "granularity": _SlotInput(
                None,
                "unset",
                None,
                "requested_slot_absent",
            ),
            "relation": _request_only_slot(relation, "bq_slots"),
            "scope": _request_only_slot(scope, "market_scope_intent"),
        }
    )
    return requested


def _request_slot(
    value: str | None,
    extractor: str,
    *,
    inherited: bool = False,
    default_suppressed: bool = False,
) -> _SlotInput:
    if inherited:
        return _SlotInput(value, "inherited", "anaphora", "extracted")
    if value is not None:
        return _SlotInput(value, "explicit", extractor, "extracted")
    return _SlotInput(
        None,
        "unset",
        extractor,
        "default_suppressed" if default_suppressed else "not_present",
    )


def _request_only_slot(value: str | None, extractor: str) -> _SlotInput:
    if value is None:
        return _SlotInput(None, "unset", extractor, "not_present")
    return _SlotInput(value, "explicit", extractor, "extracted")


def _served_slots(
    calls: tuple[Mapping[str, Any], ...],
    facts: tuple[EvidenceFact, ...],
    conversation_slots: ConversationSlots,
) -> dict[str, str | None]:
    return {
        "entity": _unique_fact_value(facts, "entity")
        or _text(conversation_slots.anchor_brand),
        "source": served_source_from_calls(calls),
        "metric": _normalize_metric(_unique_fact_value(facts, "metric"))
        or _normalize_metric(conversation_slots.metric),
        "view": _served_view(facts, conversation_slots),
        "period": _unique_fact_value(facts, "period")
        or _text(conversation_slots.period),
    }


def _served_view(
    facts: tuple[EvidenceFact, ...],
    conversation_slots: ConversationSlots,
) -> str | None:
    values = {
        value
        for fact in facts
        if (value := _canonical_public_view(fact.view, fact.market_id)) is not None
    }
    if len(values) == 1:
        return next(iter(values))
    return _canonical_public_view(conversation_slots.view, conversation_slots.market)


def _canonical_public_view(raw_view: object, raw_market: object) -> str | None:
    label = public_view(raw_view, raw_market)
    if label == MISSING_LABEL:
        return None
    if "competitive_dynamics" in label:
        return "competitive_dynamics"
    if "market_landscape" in label:
        return "market_landscape"
    if "일반뷰" in label:
        return "general_view"
    return None


def _explicit_metric(question: str) -> str | None:
    lowered = question.casefold()
    if "hhi" in lowered:
        return "hhi"
    if "cr5" in lowered or "cr 5" in lowered:
        return "cr5"
    if "집중도" in question:
        return "concentration"
    if "점유율" in question or "시장점유율" in question:
        return "market_share"
    if "순위" in question or "몇 위" in question:
        return "rank"
    metrics = tuple(
        metric
        for metric in request_metrics(question)
        if metric in _METRIC_ALLOW and metric not in {"market", "sales"}
    )
    return metrics[0] if len(metrics) == 1 else None


def _normalize_metric(value: object) -> str | None:
    normalized = str(value or "").strip().casefold()
    aliases = {
        "매출": "sales",
        "판매": "sales",
        "환자수": "patient_count",
        "환자 수": "patient_count",
        "점유율": "market_share",
        "시장점유율": "market_share",
        "시장 규모": "market_size",
        "시장규모": "market_size",
        "순위": "rank",
        "집중도": "concentration",
    }
    candidate = aliases.get(normalized, normalized)
    return candidate if candidate in _METRIC_ALLOW else None


def _public_requested_value(slot: str, value: str | None) -> str | None:
    if slot == "source" and value in _SOURCE_ALLOW:
        return value
    if slot == "metric" and value in _METRIC_ALLOW:
        return value
    if slot == "view" and value in _VIEW_ALLOW:
        return value
    return None


def _comparison(requested: str | None, served: str | None) -> Comparison:
    if requested is None or served is None:
        return "unverifiable"
    return "match" if requested == served else "mismatch"


def _present_in_answer(
    slot: str,
    value: str | None,
    answer: str,
) -> bool | None:
    if value is None:
        return None
    if slot == "source":
        terms = _SOURCE_ANSWER_TERMS.get(value)
    elif slot == "metric":
        terms = _METRIC_ANSWER_TERMS.get(value)
    elif slot == "view":
        terms = _VIEW_ANSWER_TERMS.get(value)
    else:
        terms = (value,)
    if not terms:
        return None
    answer_lower = answer.casefold()
    return any(term.casefold() in answer_lower for term in terms)


def _unique_fact_value(
    facts: tuple[EvidenceFact, ...],
    attribute: str,
) -> str | None:
    values = {
        value
        for fact in facts
        if (value := _text(getattr(fact, attribute, ""))) is not None
    }
    return next(iter(values)) if len(values) == 1 else None


def _calls(result: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    calls = result.get("tool_calls")
    if not isinstance(calls, Sequence) or isinstance(calls, (str, bytes)):
        return ()
    return tuple(call for call in calls if isinstance(call, Mapping))


def _text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None
