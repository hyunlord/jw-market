from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TypeAlias


JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


class CsdPresence(StrEnum):
    """Describe whether a PL group member exists in source CSD rows."""

    PRESENT = "present"
    ABSENT_IN_CSD = "absent_in_csd"


@dataclass(frozen=True, slots=True)
class GroupMember:
    """One PL-defined group member mapped to the IQVIA English anchor when known."""

    kr_brand: str
    iqvia_en: str | None
    status: CsdPresence
    source_markets: tuple[str, ...]
    atc4: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SourceMarket:
    """A preserved source CSD market inside a display/grouping market."""

    source_market: str
    atc4: tuple[str, ...]
    members: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MarketGroup:
    """PL-defined display group that adds grouping without replacing source market."""

    group_id: str
    label: str
    filter_label: str
    members: tuple[GroupMember, ...]
    source_markets: tuple[SourceMarket, ...]
    atc4_set: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MarketGroupModel:
    """Complete market group model used by filters and keyword/meeting bridges."""

    groups: dict[str, MarketGroup]


@dataclass(frozen=True, slots=True)
class FilterOption:
    """One market option exposed after a brand is selected."""

    option_id: str
    label: str
    option_type: str
    source_markets: tuple[str, ...]
    atc4_set: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class KeywordRow:
    """Keyword source row carried only in memory for prompt construction."""

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
    """Market-common topic axis item returned by GenOS."""

    topic_id: str
    label: str
    definition: str
    keywords: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """GenOS serving endpoint selected for the comparison."""

    model_key: str
    serving_id: str
    label: str


@dataclass(frozen=True, slots=True)
class ScopeSpec:
    """One bounded comparison scope for axis and brand-share calls."""

    scope_id: str
    label: str
    atc4_values: tuple[str, ...]
    axis_brands: tuple[str, ...]
    share_brands: tuple[tuple[str, str], ...]
    scope_type: str


@dataclass(frozen=True, slots=True)
class CallLog:
    """Sanitized record of one GenOS call."""

    task: str
    model_key: str
    serving_id: str
    scope_id: str
    brand: str
    status: str
    latency_ms: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_input_tokens: int
    input_hash: str
    output_sha256: str
    output_length: int
