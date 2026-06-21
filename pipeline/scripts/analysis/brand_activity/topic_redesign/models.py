"""Typed records shared by the topic redesign analysis."""

from __future__ import annotations

from dataclasses import dataclass


JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


@dataclass(frozen=True, slots=True)
class MessageRow:
    """One source text row read from the local stage database."""

    source: str
    row_id: str
    market: str
    period_ym: str
    product_name: str
    text: str
    stage_hash: str


@dataclass(frozen=True, slots=True)
class LabelTemplate:
    """Human-reviewable provisional label seed with discovery trigger terms."""

    label: str
    keywords: tuple[str, ...]
    source: str
    note: str


@dataclass(frozen=True, slots=True)
class LabelCandidate:
    """Measured candidate label generated for one ATC4 market."""

    market: str
    label: str
    keywords: tuple[str, ...]
    evidence_terms: tuple[str, ...]
    source: str
    hit_count: int
    coverage_rate: float
    snippets: tuple[str, ...]
    note: str


@dataclass(frozen=True, slots=True)
class MethodScore:
    """Comparable score for one extraction method on one sample market."""

    market: str
    method: str
    candidate_count: int
    coverage_rate: float
    noise_rate: float
    redundancy_rate: float
    score: float
    top_candidates: tuple[str, ...]
    note: str


@dataclass(frozen=True, slots=True)
class CoverageRow:
    """Dictionary reclassification coverage for one ATC4 market."""

    market: str
    rows: int
    matched_rows: int
    unmatched_rows: int
    multilabel_rows: int
    unmatched_rate: float
    multilabel_rate: float

