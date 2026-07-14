from __future__ import annotations

from dataclasses import dataclass, replace
import time
from collections.abc import Callable
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class SeriesPoint:
    period: str
    value_krw: float | None = None
    ms_pct: float | None = None
    rank: int | None = None


@dataclass(frozen=True, slots=True)
class RankedBrandSlot:
    brand: str
    rank: int | None = None
    series: tuple[SeriesPoint, ...] = ()


@dataclass(frozen=True, slots=True)
class ConversationSlots:
    anchor_brand: str | None = None
    market: str | None = None
    market_definition: str | None = None
    period: str | None = None
    denominator: str | None = None
    ranked_brands: tuple[str, ...] = ()
    ranked: tuple[RankedBrandSlot, ...] = ()
    file_name: str | None = None
    file_measure: str | None = None


@dataclass(frozen=True, slots=True)
class ConversationTurn:
    question: str
    answer: str
    applied_filters: tuple[tuple[str, str], ...] = ()
    slots: ConversationSlots = ConversationSlots()


@dataclass(frozen=True, slots=True)
class PendingClarification:
    kind: str
    original_question: str
    brand: str
    metric: str
    created_at: float
    expires_at: float


@dataclass(frozen=True, slots=True)
class ConversationState:
    conversation_id: str
    turns: tuple[ConversationTurn, ...] = ()
    pending: PendingClarification | None = None
    updated_at: float = 0.0


class ConversationStore:
    def __init__(
        self,
        *,
        max_turns: int = 5,
        ttl_seconds: int = 600,
        pending_ttl_seconds: int = 180,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._max_turns = max(1, max_turns)
        self._ttl_seconds = max(1, ttl_seconds)
        self._pending_ttl_seconds = max(1, pending_ttl_seconds)
        self._clock = clock or time.monotonic
        self._states: dict[str, ConversationState] = {}

    @property
    def pending_ttl_seconds(self) -> int:
        return self._pending_ttl_seconds

    def get_or_create(self, conversation_id: str | None = None) -> ConversationState:
        state_id = conversation_id or uuid4().hex
        current = self._states.get(state_id)
        now = self._clock()
        if current is None or now - current.updated_at > self._ttl_seconds:
            current = ConversationState(conversation_id=state_id, updated_at=now)
            self._states[state_id] = current
            return current
        current = self._without_expired_pending(current, now)
        self._states[state_id] = current
        return current

    def get_pending(self, conversation_id: str) -> PendingClarification | None:
        state = self.get_or_create(conversation_id)
        return state.pending

    def set_pending(self, conversation_id: str, pending: PendingClarification) -> None:
        state = self.get_or_create(conversation_id)
        self._states[conversation_id] = replace(state, pending=pending, updated_at=self._clock())

    def clear_pending(self, conversation_id: str) -> None:
        state = self.get_or_create(conversation_id)
        self._states[conversation_id] = replace(state, pending=None, updated_at=self._clock())

    def record_exchange(
        self,
        conversation_id: str,
        question: str,
        answer: str,
        applied_filters: tuple[tuple[str, str], ...] = (),
        *,
        slots: ConversationSlots = ConversationSlots(),
    ) -> None:
        state = self.get_or_create(conversation_id)
        turns = (
            *state.turns,
            ConversationTurn(question=question, answer=answer, applied_filters=applied_filters, slots=slots),
        )
        trimmed = turns[-self._max_turns :]
        self._states[conversation_id] = replace(state, turns=trimmed, pending=state.pending, updated_at=self._clock())

    def pending_expiry(self) -> float:
        return self._clock() + self._pending_ttl_seconds

    @staticmethod
    def _without_expired_pending(state: ConversationState, now: float) -> ConversationState:
        pending = state.pending
        if pending is not None and pending.expires_at <= now:
            return replace(state, pending=None, updated_at=now)
        return state
