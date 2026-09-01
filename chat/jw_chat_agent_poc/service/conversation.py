from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
import sys
import threading
import time
from typing import Any
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
class ResultReference:
    tool: str
    source: str | None = None
    brand: str | None = None
    market: str | None = None
    period: str | None = None


@dataclass(frozen=True, slots=True)
class ConversationSlots:
    anchor_brand: str | None = None
    # True when anchor_brand came from the standalone market golden rewrite instead of
    # from the user. That value is a question-rewriting device, so a later pronoun must
    # not inherit it as though the user had named the brand.
    anchor_brand_is_synthetic: bool = False
    market: str | None = None
    market_definition: str | None = None
    period: str | None = None
    metric: str | None = None
    view: str | None = None
    result_ref: ResultReference | None = None
    denominator: str | None = None
    ranked_brands: tuple[str, ...] = ()
    ranked: tuple[RankedBrandSlot, ...] = ()
    file_name: str | None = None
    file_measure: str | None = None
    file_manufacturer: str | None = None
    file_sheet: str | None = None
    # Headlines or numeric trends this turn actually put in front of the user. A
    # following '왜 이렇게 됐어?' names no subject of its own: what it asks about is
    # whatever was just shown. Nothing about the observation was recorded before, so the
    # cause question could only ever be read as a standalone one.
    issue_observation: tuple[str, ...] = ()


def conversation_slots_to_dict(slots: ConversationSlots) -> dict[str, Any]:
    return {
        "anchor_brand": slots.anchor_brand,
        "anchor_brand_is_synthetic": slots.anchor_brand_is_synthetic,
        "market": slots.market,
        "market_definition": slots.market_definition,
        "period": slots.period,
        "metric": slots.metric,
        "view": slots.view,
        "result_ref": (
            {
                "tool": slots.result_ref.tool,
                "source": slots.result_ref.source,
                "brand": slots.result_ref.brand,
                "market": slots.result_ref.market,
                "period": slots.result_ref.period,
            }
            if slots.result_ref is not None
            else None
        ),
        "denominator": slots.denominator,
        "ranked_brands": list(slots.ranked_brands),
        "ranked": [
            {
                "brand": item.brand,
                "rank": item.rank,
                "series": [
                    {
                        "period": point.period,
                        "value_krw": point.value_krw,
                        "ms_pct": point.ms_pct,
                        "rank": point.rank,
                    }
                    for point in item.series
                ],
            }
            for item in slots.ranked
        ],
        "issue_observation": list(slots.issue_observation),
        "file_name": slots.file_name,
        "file_measure": slots.file_measure,
        "file_manufacturer": slots.file_manufacturer,
        "file_sheet": slots.file_sheet,
    }


def conversation_slots_from_dict(value: object) -> ConversationSlots:
    if not isinstance(value, dict):
        return ConversationSlots()
    ranked: list[RankedBrandSlot] = []
    for item in value.get("ranked", []):
        if not isinstance(item, dict) or not _optional_text(item.get("brand")):
            continue
        series: list[SeriesPoint] = []
        for point in item.get("series", []):
            if not isinstance(point, dict) or not _optional_text(point.get("period")):
                continue
            series.append(
                SeriesPoint(
                    period=_optional_text(point.get("period")) or "",
                    value_krw=_optional_float(point.get("value_krw")),
                    ms_pct=_optional_float(point.get("ms_pct")),
                    rank=_optional_int(point.get("rank")),
                )
            )
        ranked.append(
            RankedBrandSlot(
                brand=_optional_text(item.get("brand")) or "",
                rank=_optional_int(item.get("rank")),
                series=tuple(series),
            )
        )
    ranked_brands = value.get("ranked_brands")
    issue_observation = value.get("issue_observation")
    result_ref_value = value.get("result_ref")
    result_ref = (
        ResultReference(
            tool=_optional_text(result_ref_value.get("tool")) or "",
            source=_optional_text(result_ref_value.get("source")),
            brand=_optional_text(result_ref_value.get("brand")),
            market=_optional_text(result_ref_value.get("market")),
            period=_optional_text(result_ref_value.get("period")),
        )
        if isinstance(result_ref_value, dict) and _optional_text(result_ref_value.get("tool"))
        else None
    )
    return ConversationSlots(
        anchor_brand=_optional_text(value.get("anchor_brand")),
        # A turn stored before this field existed has no key, so absence means
        # "user-chosen" — the same reading the old code gave every anchor.
        anchor_brand_is_synthetic=bool(value.get("anchor_brand_is_synthetic")),
        market=_optional_text(value.get("market")),
        market_definition=_optional_text(value.get("market_definition")),
        period=_optional_text(value.get("period")),
        metric=_optional_text(value.get("metric")),
        view=_optional_text(value.get("view")),
        result_ref=result_ref,
        denominator=_optional_text(value.get("denominator")),
        ranked_brands=(
            tuple(text for item in ranked_brands if (text := _optional_text(item)))
            if isinstance(ranked_brands, list)
            else ()
        ),
        ranked=tuple(ranked),
        # A turn stored before this field existed has no key, so absence means "no
        # observation was shown" — which is what every stored turn meant until now.
        issue_observation=(
            tuple(text for item in issue_observation if (text := _optional_text(item)))
            if isinstance(issue_observation, list)
            else ()
        ),
        file_name=_optional_text(value.get("file_name")),
        file_measure=_optional_text(value.get("file_measure")),
        file_manufacturer=_optional_text(value.get("file_manufacturer")),
        file_sheet=_optional_text(value.get("file_sheet")),
    )


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: object) -> int | None:
    number = _optional_float(value)
    return int(number) if number is not None and number.is_integer() else None


@dataclass(frozen=True, slots=True)
class ConversationTurn:
    question: str
    answer: str
    applied_filters: tuple[tuple[str, str], ...] = ()
    slots: ConversationSlots = ConversationSlots()
    trace: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DiseaseCodeCandidateSlot:
    sick_cd: str
    disease_name: str


@dataclass(frozen=True, slots=True)
class PendingClarification:
    kind: str
    original_question: str
    brand: str
    metric: str
    created_at: float
    expires_at: float
    disease_candidates: tuple[DiseaseCodeCandidateSlot, ...] = ()


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
        max_states: int = 500,
        ttl_seconds: int = 600,
        pending_ttl_seconds: int = 180,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._max_turns = max(1, max_turns)
        self._max_states = max(1, max_states)
        self._ttl_seconds = max(1, ttl_seconds)
        self._pending_ttl_seconds = max(1, pending_ttl_seconds)
        self._clock = clock or time.monotonic
        self._states: OrderedDict[str, ConversationState] = OrderedDict()
        self._lock = threading.RLock()
        self._capacity_evictions = 0
        self._ttl_evictions = 0
        self._sweep_interval_seconds = min(self._ttl_seconds, 60)
        self._next_sweep_at = self._clock() + self._sweep_interval_seconds

    @property
    def pending_ttl_seconds(self) -> int:
        return self._pending_ttl_seconds

    def get_or_create(self, conversation_id: str | None = None) -> ConversationState:
        with self._lock:
            state_id = conversation_id or uuid4().hex
            now = self._clock()
            self._sweep_expired(now)
            current = self._states.get(state_id)
            if current is None:
                current = ConversationState(conversation_id=state_id, updated_at=now)
                self._store(state_id, current)
                return current
            current = self._without_expired_pending(current, now)
            self._store(state_id, current)
            return current

    def get_pending(self, conversation_id: str) -> PendingClarification | None:
        state = self.get_or_create(conversation_id)
        return state.pending

    def set_pending(self, conversation_id: str, pending: PendingClarification) -> None:
        with self._lock:
            state = self.get_or_create(conversation_id)
            self._store(conversation_id, replace(state, pending=pending, updated_at=self._clock()))

    def clear_pending(self, conversation_id: str) -> None:
        with self._lock:
            state = self.get_or_create(conversation_id)
            self._store(conversation_id, replace(state, pending=None, updated_at=self._clock()))

    def record_exchange(
        self,
        conversation_id: str,
        question: str,
        answer: str,
        applied_filters: tuple[tuple[str, str], ...] = (),
        *,
        slots: ConversationSlots = ConversationSlots(),
        trace: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            state = self.get_or_create(conversation_id)
            turns = (
                *state.turns,
                ConversationTurn(
                    question=question,
                    answer=answer,
                    applied_filters=applied_filters,
                    slots=slots,
                    trace=dict(trace or {}),
                ),
            )
            trimmed = turns[-self._max_turns :]
            self._store(
                conversation_id,
                replace(state, turns=trimmed, pending=state.pending, updated_at=self._clock()),
            )

    def pending_expiry(self) -> float:
        return self._clock() + self._pending_ttl_seconds

    def observability(self) -> dict[str, int]:
        with self._lock:
            self._sweep_expired(self._clock(), force=True)
            states = tuple(self._states.values())
            return {
                "state_count": len(states),
                "turn_count": sum(len(state.turns) for state in states),
                "max_states": self._max_states,
                "capacity_evictions": self._capacity_evictions,
                "ttl_evictions": self._ttl_evictions,
                "approx_bytes": _bounded_size(states),
            }

    def _store(self, state_id: str, state: ConversationState) -> None:
        self._states[state_id] = state
        self._states.move_to_end(state_id)
        while len(self._states) > self._max_states:
            self._states.popitem(last=False)
            self._capacity_evictions += 1

    def _sweep_expired(self, now: float, *, force: bool = False) -> None:
        if not force and now < self._next_sweep_at:
            return
        expired_ids = [
            state_id
            for state_id, state in self._states.items()
            if now - state.updated_at > self._ttl_seconds
        ]
        for state_id in expired_ids:
            del self._states[state_id]
            self._ttl_evictions += 1
        self._next_sweep_at = now + self._sweep_interval_seconds

    @staticmethod
    def _without_expired_pending(state: ConversationState, now: float) -> ConversationState:
        pending = state.pending
        if pending is not None and pending.expires_at <= now:
            return replace(state, pending=None, updated_at=now)
        return state


def _bounded_size(states: tuple[ConversationState, ...], *, sample_limit: int = 32) -> int:
    sampled = states[:sample_limit]
    size = sys.getsizeof(states)
    for state in sampled:
        size += sys.getsizeof(state) + sys.getsizeof(state.turns)
        for turn in state.turns:
            size += (
                sys.getsizeof(turn)
                + sys.getsizeof(turn.question)
                + sys.getsizeof(turn.answer)
                + sys.getsizeof(turn.applied_filters)
                + sys.getsizeof(turn.slots)
            )
        if state.pending is not None:
            size += sys.getsizeof(state.pending)
    if sampled and len(sampled) < len(states):
        size = round(size / len(sampled) * len(states))
    return size
