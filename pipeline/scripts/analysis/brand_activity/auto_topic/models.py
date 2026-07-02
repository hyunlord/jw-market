from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias


JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


@dataclass(frozen=True, slots=True)
class KeywordRow:
    """One Keyword stage row kept in memory only for prompt construction."""

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
    representing_company: str = ""


@dataclass(frozen=True, slots=True)
class TopicDefinition:
    """One topic on a market-common axis."""

    topic_id: str
    label: str
    definition: str
    keywords: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """GenOS serving endpoint metadata used by the bounded PoC."""

    model_key: str
    serving_id: str
    label: str


@dataclass(frozen=True, slots=True)
class BrandDescription:
    """Alias-derived brand metadata used only as prompt context."""

    brand: str
    atc4: str
    kr_canonical: str | None
    is_jw: bool
    molecule: tuple[str, ...]
    manufacturer: tuple[str, ...]
    representing_company: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CallLog:
    """Sanitized GenOS call metadata without raw prompt or raw response text."""

    task: str
    model_key: str
    serving_id: str
    scope_id: str
    atc4: str
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
    error_type: str = ""
    error_message: str = ""
    phase: str = ""
    ttfb_ms: int = 0
    read_ms: int = 0
    connect_timeout_s: float = 0.0
    read_timeout_s: float = 0.0
    watchdog_timeout_s: float = 0.0
    attempts: int = 1
    retry_count: int = 0
    retry_reasons: tuple[str, ...] = ()
    backend: str = "direct_serving"
    endpoint: str = ""
    model_id: str = ""
