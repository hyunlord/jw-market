from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypeAlias

JsonValue: TypeAlias = (
    None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
)


@dataclass(frozen=True, slots=True)
class GoldKey:
    label: str
    key: str
    kind: str


@dataclass(frozen=True, slots=True)
class EvalQuestion:
    question_id: str
    category: str
    question: str
    gold_note: str
    expected_behavior: str
    gold_keys: tuple[GoldKey, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class RawResult:
    question_id: str
    ok: bool
    result: dict[str, JsonValue]
    error: str | None = None


@dataclass(frozen=True, slots=True)
class GoldObservation:
    label: str
    key: str
    kind: str
    value: int | float | str


@dataclass(frozen=True, slots=True)
class ScoredRow:
    question: EvalQuestion
    answer: str
    numeric_accuracy: str
    qualitative_score: int
    note: str
    gold_observations: tuple[GoldObservation, ...]
    ok: bool
