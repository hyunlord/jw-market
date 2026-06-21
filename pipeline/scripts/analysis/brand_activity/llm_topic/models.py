from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict


JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


@dataclass(frozen=True, slots=True)
class KeywordRow:
    row_id: int
    period_ym: str
    atc4: str
    brand: str
    keyword_text: str
    interest: str
    prescription_frequency: str
    prescription_evolution: str
    promotional_lit: str
    abstract_lit: str
    patient_lit: str
    specialty: str
    visit_location: str
    stage_row_sha256: str


@dataclass(frozen=True, slots=True)
class TopicDefinition:
    topic_id: str
    label: str
    definition: str
    keywords: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TopicShare:
    topic_id: str
    label: str
    share_pct: float
    row_count: int


@dataclass(frozen=True, slots=True)
class BrandDescription:
    brand: str
    atc4: str
    kr_canonical: str | None
    molecule: tuple[str, ...]
    is_jw: bool
    manufacturer: tuple[str, ...]
    representing_company: tuple[str, ...]


class RedactedAuditRow(TypedDict):
    row_id: int
    period_ym: str
    atc4: str
    brand: str
    text_sha256: str
    text_length: int
    estimated_tokens: int
    interest: str
    prescription_frequency: str
    prescription_evolution: str
    promotional_lit: str
    abstract_lit: str
    patient_lit: str
    specialty: str
    visit_location: str
    stage_row_sha256: str


class TopicShareItem(TypedDict):
    topic_id: str
    label: str
    share_pct: float
    row_count: int


class BrandSharePayload(TypedDict):
    brand: str
    atc4: str
    axis_version: str
    denominator: str
    row_count: int
    topic_shares: list[TopicShareItem]
    etc_pct: float
    evidence_note: str
