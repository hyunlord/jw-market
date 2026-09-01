from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from jw_chat_agent_poc.service.conversation import (
    ConversationTurn,
    conversation_slots_to_dict,
)

_SCALAR_SLOT_KEYS = (
    "anchor_brand",
    "market",
    "market_definition",
    "period",
    "metric",
    "view",
    "denominator",
    "file_name",
    "file_measure",
    "file_manufacturer",
    "file_sheet",
)
_LIST_SLOT_KEYS = ("ranked_brands",)
_SUMMARY_KEYS = (
    "anchor_brand",
    "market",
    "period",
    "metric",
    "view",
    "denominator",
    "file_name",
    "file_measure",
)
_TEXT_LIMIT = 240
_SUMMARY_LIMIT = 600


def project_recent_turns(
    turns: Sequence[ConversationTurn],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Project only user text and code-owned structured context for later turns."""

    if limit <= 0:
        return []
    return [_project_turn(turn) for turn in tuple(turns)[-limit:]]


def _project_turn(turn: ConversationTurn) -> dict[str, Any]:
    raw_slots = conversation_slots_to_dict(turn.slots)
    slots = _question_owned_slots(turn.question, raw_slots)
    confirmed_context: dict[str, Any] = {}
    if slots:
        confirmed_context["slots"] = slots
    return {
        "user_question": turn.question,
        "confirmed_context": confirmed_context,
        "prior_answer_summary": _structured_summary(slots),
    }


def _question_owned_slots(question: str, raw_slots: dict[str, Any]) -> dict[str, Any]:
    normalized_question = _normalized(question)
    slots = {
        key: cleaned
        for key in _SCALAR_SLOT_KEYS
        if (cleaned := _clean(raw_slots.get(key))) is not None
        and _normalized(cleaned) in normalized_question
    }
    for key in _LIST_SLOT_KEYS:
        values = _clean(raw_slots.get(key))
        if not isinstance(values, list):
            continue
        owned = [value for value in values if _normalized(value) in normalized_question]
        if owned:
            slots[key] = owned
    return slots


def _structured_summary(slots: dict[str, Any]) -> str:
    parts = [
        f"{key}={_summary_value(slots[key])}"
        for key in _SUMMARY_KEYS
        if key in slots
    ]
    summary = "; ".join(parts) or "확정된 구조화 요약 없음"
    return summary[:_SUMMARY_LIMIT]


def _summary_value(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(_summary_value(item) for item in value[:8])
    if isinstance(value, dict):
        return ", ".join(
            f"{key}:{_summary_value(item)}" for key, item in list(value.items())[:8]
        )
    return _bounded_text(value)


def _clean(value: Any) -> Any | None:
    if value is None or value is False:
        return None
    if isinstance(value, str):
        text = _bounded_text(value)
        return text or None
    if isinstance(value, dict):
        cleaned = {
            _bounded_text(key): item
            for key, raw in value.items()
            if (item := _clean(raw)) is not None
        }
        return cleaned or None
    if isinstance(value, (list, tuple)):
        cleaned_items = [item for raw in value if (item := _clean(raw)) is not None]
        return cleaned_items[:16] or None
    return value


def _bounded_text(value: Any) -> str:
    return str(value).strip()[:_TEXT_LIMIT]


def _normalized(value: Any) -> str:
    return "".join(_bounded_text(value).casefold().split())
