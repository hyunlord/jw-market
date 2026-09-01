from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from jw_chat_agent_poc.service.conversation import ConversationTurn

if TYPE_CHECKING:
    from jw_chat_agent_poc.service.v4.contracts import PlannerOutput, SourceResult

PRIOR_TURN_LIMIT = 3
PRIOR_TURN_EVIDENCE_ID = "prior_turn:1:1:1"

_REFERENCE_RE = re.compile(r"(?:이건|이거|그거|아까|방금|저\s*수치|그럼)")
_HISTORICAL_VALUE_RE = re.compile(r"(?:아까|방금).*(?:얼마|몇|수치|값)")
_AXIS_ADDITION_RE = re.compile(
    r"^(?:그럼\s*)?(?:성별|연령별|연령대별|기간별|월별|연도별|회사별|단계별|상태별)(?:로는|은|는|로|별)?[?？]?$"
)
_IDENTIFIER_RE = re.compile(
    r"NCT\d{8}|10-\d{6,8}|\b[A-Z]\d{2}[._]?\d?\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class PriorTurnContext:
    triggered: bool
    reason: str
    historical_value_request: bool
    turn_limit: int
    items: tuple[dict[str, Any], ...]
    merge_entities: tuple[str, ...]
    result: SourceResult | None
    trace: dict[str, Any]


def build_prior_turn_context(
    question: str,
    turns: Sequence[ConversationTurn],
) -> PriorTurnContext:
    normalized = " ".join(question.split())
    selected = tuple(reversed(tuple(turns)[-PRIOR_TURN_LIMIT:]))
    reason = _trigger_reason(normalized, selected)
    triggered = reason != "new_topic"
    historical = bool(_HISTORICAL_VALUE_RE.search(normalized))
    explicit_comparison = normalized.startswith("그럼 ") and not historical

    items: list[dict[str, Any]] = []
    merge_entities: list[str] = []
    if triggered:
        for turns_ago, turn in enumerate(selected, start=1):
            identifiers = _identifiers(f"{turn.question}\n{turn.answer}")
            resolved_entities = _resolved_entities(turn)
            inherited_entities = _inherited_entities(
                turn,
                resolved_entities,
                replace_anchor_brand=explicit_comparison,
            )
            merge_entities.extend((*inherited_entities, *identifiers))
            item: dict[str, Any] = {
                "turns_ago": turns_ago,
                "previous_question": turn.question,
                "previous_answered_at": str(turn.trace.get("_history_created_at") or ""),
                "carried_items": list(dict.fromkeys((*resolved_entities, *identifiers))),
                "identifiers": list(identifiers),
                "original_sources": list(_original_sources(turn)),
                "question_shape": _question_shape(turn),
                "requery_required": not historical,
                "requeried": False,
                "requery_sources": [],
                "reused_evidence_ids": [],
            }
            if historical:
                excerpt = _numeric_excerpt(turn.answer)
                if excerpt:
                    item["historical_excerpt"] = excerpt
            items.append(item)

    result = None
    if triggered and items:
        from jw_chat_agent_poc.service.v4.contracts import SourceResult

        result = SourceResult(
            source="prior_turn",
            query=normalized,
            status="ok",
            payload={
                "items": items,
                "policy": "historical_value" if historical else "entity_context_requery",
                "turn_limit": PRIOR_TURN_LIMIT,
            },
            notice="이전 대화에서 참조 근거 확인",
        )
    unique_entities = tuple(dict.fromkeys(value for value in merge_entities if value))
    return PriorTurnContext(
        triggered=triggered,
        reason=reason,
        historical_value_request=historical,
        turn_limit=PRIOR_TURN_LIMIT,
        items=tuple(items),
        merge_entities=unique_entities,
        result=result,
        trace={
            "triggered": triggered,
            "reason": reason,
            "turn_limit": PRIOR_TURN_LIMIT,
            "turns_available": len(turns),
            "turns_carried": len(items),
            "historical_value_request": historical,
            "requery_required": bool(triggered and not historical),
            "reused_evidence_ids": 0,
        },
    )


def merge_prior_turn_entities(
    plan: PlannerOutput,
    entities: Sequence[str],
) -> PlannerOutput:
    merged = tuple(
        dict.fromkeys(
            (*plan.answer_contract.resolved_entities, *plan.requested_answer_shape.entities, *entities)
        )
    )
    sources = tuple(dict.fromkeys((*plan.answer_sources, "prior_turn")))
    return plan.model_copy(
        update={
            "answer_sources": sources,
            "expanded_intents": tuple(
                dict.fromkeys((*plan.expanded_intents, "이전 대화에서 참조 근거 확인"))
            ),
            "answer_contract": plan.answer_contract.model_copy(
                update={"resolved_entities": merged}
            ),
            "requested_answer_shape": plan.requested_answer_shape.model_copy(
                update={"entities": merged}
            ),
        }
    )


def append_prior_turn_annotation(text: str, context: PriorTurnContext) -> str:
    if not context.triggered or not context.items or "[출처: 이전 답변" in text:
        return text
    item = context.items[0]
    turns_ago = int(item.get("turns_ago") or 1)
    marker = f"[출처: 이전 답변 · {turns_ago}턴 전]"
    if context.historical_value_request:
        excerpt = str(item.get("historical_excerpt") or "이전 답변의 수치")
        note = f"그 시점 기준으로 이전 답변에는 {excerpt} {marker}"
    elif bool(item.get("requeried")):
        note = f"이전 대화에서 이어받은 실체는 이번 턴 원천 조회로 다시 확인했습니다. {marker}"
    else:
        note = f"이전 대화에서 이어받은 실체는 이번 턴 원천에서 다시 확인하지 못했습니다. {marker}"
    return f"{text.rstrip()}\n\n{note}".strip()


def prior_turn_evidence_reference(context: PriorTurnContext) -> dict[str, str]:
    turns_ago = int(context.items[0]["turns_ago"]) if context.items else 1
    return {
        "evidence_id": PRIOR_TURN_EVIDENCE_ID,
        "label": f"출처: 이전 답변 · {turns_ago}턴 전",
    }


def finalize_prior_turn_requery(
    context: PriorTurnContext,
    current_results: Sequence[SourceResult],
) -> PriorTurnContext:
    if not context.triggered or context.historical_value_request or context.result is None:
        return context
    fresh_sources = tuple(
        dict.fromkeys(
            result.source
            for result in current_results
            if result.source != "prior_turn" and result.status == "ok"
        )
    )
    items = tuple(
        {
            **item,
            "requeried": bool(fresh_sources),
            "requery_sources": list(fresh_sources),
        }
        for item in context.items
    )
    result = context.result.model_copy(
        update={
            "payload": {
                **dict(context.result.payload or {}),
                "items": list(items),
            }
        }
    )
    return replace(
        context,
        items=items,
        result=result,
        trace={
            **context.trace,
            "requery_completed": bool(fresh_sources),
            "requery_sources": list(fresh_sources),
        },
    )


def allow_legacy_result_reuse(context: PriorTurnContext) -> bool:
    """Never bind a previous turn's source result to a current follow-up."""

    return not context.triggered


def _trigger_reason(question: str, turns: Sequence[ConversationTurn]) -> str:
    if not turns:
        return "new_topic"
    identifiers = _identifiers(question)
    prior_text = "\n".join(turn.answer for turn in turns)
    if identifiers and any(identifier.casefold() in prior_text.casefold() for identifier in identifiers):
        return "prior_identifier"
    if _AXIS_ADDITION_RE.match(question):
        return "axis_addition"
    if _REFERENCE_RE.search(question):
        return "reference_expression"
    return "new_topic"


def _resolved_entities(turn: ConversationTurn) -> tuple[str, ...]:
    values: list[str] = []
    planner = turn.trace.get("planner")
    if isinstance(planner, Mapping):
        contract = planner.get("answer_contract")
        if isinstance(contract, Mapping):
            raw = contract.get("resolved_entities")
            if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
                values.extend(str(item).strip() for item in raw if str(item).strip())
    slots = turn.slots
    values.extend(
        value
        for value in (
            slots.anchor_brand,
            slots.market,
            slots.period,
            slots.metric,
            slots.file_name,
            slots.file_sheet,
        )
        if value
    )
    return tuple(dict.fromkeys(values))


def _inherited_entities(
    turn: ConversationTurn,
    resolved_entities: Sequence[str],
    *,
    replace_anchor_brand: bool,
) -> tuple[str, ...]:
    values = list(resolved_entities)
    if replace_anchor_brand and turn.slots.anchor_brand:
        values = [value for value in values if value != turn.slots.anchor_brand]
    values.extend(
        value
        for value in (turn.slots.period, turn.slots.metric, turn.slots.market)
        if value
    )
    return tuple(dict.fromkeys(values))


def _question_shape(turn: ConversationTurn) -> dict[str, str]:
    return {
        key: value
        for key, value in (
            ("period", turn.slots.period),
            ("metric", turn.slots.metric),
            ("market", turn.slots.market),
            ("file_name", turn.slots.file_name),
            ("file_sheet", turn.slots.file_sheet),
        )
        if value
    }


def _identifiers(text: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(match.group(0).upper().replace("_", ".") for match in _IDENTIFIER_RE.finditer(text)))


def _original_sources(turn: ConversationTurn) -> tuple[str, ...]:
    sections = turn.trace.get("answer_sections")
    if not isinstance(sections, Mapping):
        return ()
    catalog = sections.get("evidence_catalog")
    if not isinstance(catalog, Mapping):
        return ()
    values = []
    for entry in catalog.values():
        if isinstance(entry, Mapping) and entry.get("source_name"):
            values.append(str(entry["source_name"]))
    return tuple(dict.fromkeys(values))


def _numeric_excerpt(answer: str) -> str:
    sentences = re.split(r"(?<=[가-힣)])\.(?:\s+|$)", answer)
    return next((" ".join(sentence.split()) for sentence in sentences if re.search(r"\d", sentence)), "")


__all__ = [
    "PRIOR_TURN_EVIDENCE_ID",
    "PRIOR_TURN_LIMIT",
    "PriorTurnContext",
    "allow_legacy_result_reuse",
    "append_prior_turn_annotation",
    "build_prior_turn_context",
    "finalize_prior_turn_requery",
    "merge_prior_turn_entities",
    "prior_turn_evidence_reference",
]
