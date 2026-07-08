from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from .event_bundle_builder import CROSS_MATCH_SOURCE_PROCESSORS, DIRECT_EVENT_SOURCE_PROCESSORS

QUALITY_SCORE_CUTOFF: Final = 50
CATEGORY_SCORE_CUTOFFS: Final = {
    "자본/경영": 43,
    "외부/트렌드": 49,
    "공급/생산": 51,
    "신약/R&D": 54,
    "정책/규제": 55,
}
EXCLUDED_EVIDENCE_TAGS: Final = frozenset({"기타"})
FULL_MIN_EVIDENCE: Final = 10
MID_MIN_EVIDENCE: Final = 3
SPARSE_MIN_EVIDENCE: Final = 1
ALLOWED_PROCESSORS: Final = DIRECT_EVENT_SOURCE_PROCESSORS + CROSS_MATCH_SOURCE_PROCESSORS
ALLOWED_DERIVATIONS: Final = ("llm_direct", "cross_match")


class ProcessingMode(StrEnum):
    LLM_FULL = "llm_full"
    LLM_COMPACT = "llm_compact"
    LLM_RECAP = "llm_recap"
    TEMPLATE_ZERO = "template_zero"


@dataclass(frozen=True, slots=True)
class DensityBucket:
    bucket: str
    mode: ProcessingMode
    min_count: int
    max_count: int | None


@dataclass(frozen=True, slots=True)
class EvidenceCount:
    brand: str
    source_processor: str
    derivation: str
    count: int
    score_cutoff: int = QUALITY_SCORE_CUTOFF
    tag: str | None = None


@dataclass(frozen=True, slots=True)
class RouteDecision:
    brand: str
    evidence_count: int
    bucket: str
    mode: ProcessingMode
    included_processors: tuple[str, ...]


def density_bucket(evidence_count: int) -> DensityBucket:
    """Classify an Agent2 brand by score-cut evidence density."""

    if evidence_count >= FULL_MIN_EVIDENCE:
        return DensityBucket("full", ProcessingMode.LLM_FULL, FULL_MIN_EVIDENCE, None)
    if evidence_count >= MID_MIN_EVIDENCE:
        return DensityBucket("mid", ProcessingMode.LLM_COMPACT, MID_MIN_EVIDENCE, FULL_MIN_EVIDENCE - 1)
    if evidence_count >= SPARSE_MIN_EVIDENCE:
        return DensityBucket("sparse", ProcessingMode.LLM_RECAP, SPARSE_MIN_EVIDENCE, MID_MIN_EVIDENCE - 1)
    return DensityBucket("zero", ProcessingMode.TEMPLATE_ZERO, 0, 0)


def is_allowed_evidence(row: EvidenceCount) -> bool:
    """Return whether a score row can feed Agent2 density routing."""

    expected_cutoff = cutoff_for_tag(row.tag)
    if expected_cutoff is None:
        return False
    return (
        row.score_cutoff == expected_cutoff
        and row.source_processor in ALLOWED_PROCESSORS
        and row.derivation in ALLOWED_DERIVATIONS
    )


def cutoff_for_tag(tag: str | None) -> int | None:
    """Return the category-specific evidence cutoff, or None when excluded."""

    normalized = (tag or "").strip()
    if normalized in EXCLUDED_EVIDENCE_TAGS:
        return None
    return CATEGORY_SCORE_CUTOFFS.get(normalized, QUALITY_SCORE_CUTOFF)


def is_score_allowed_for_density(score: int | float, tag: str | None) -> bool:
    """Evaluate a raw score against the category-specific density cutoff."""

    cutoff = cutoff_for_tag(tag)
    return cutoff is not None and score >= cutoff


def route_brand(brand: str, counts: tuple[EvidenceCount, ...]) -> RouteDecision:
    """Route one brand to the LLM or zero-template lane."""

    allowed = tuple(row for row in counts if row.brand == brand and is_allowed_evidence(row))
    evidence_count = sum(max(row.count, 0) for row in allowed)
    bucket = density_bucket(evidence_count)
    processors = tuple(
        processor
        for processor in ALLOWED_PROCESSORS
        if any(row.source_processor == processor and row.count > 0 for row in allowed)
    )
    return RouteDecision(
        brand=brand,
        evidence_count=evidence_count,
        bucket=bucket.bucket,
        mode=bucket.mode,
        included_processors=processors,
    )


def route_worklist(brands: tuple[str, ...], counts: tuple[EvidenceCount, ...]) -> tuple[RouteDecision, ...]:
    """Route a mart-universe brand list while preserving input order."""

    return tuple(route_brand(brand, counts) for brand in brands)
