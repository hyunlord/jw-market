from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pipeline.etl.io.mart.agent2_eligibility import (
    AGENT2_ALLOWED_PROCESSORS,
    Agent2ScoreRow,
    is_agent2_eligible,
)


FULL_MIN_EVIDENCE = 10
MID_MIN_EVIDENCE = 3
SPARSE_MIN_EVIDENCE = 1


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
class RouteDecision:
    brand: str
    evidence_count: int
    bucket: str
    mode: ProcessingMode
    included_processors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BrandedScoreRow:
    brand_key: str
    score: Agent2ScoreRow


class UnknownScoreBrandError(RuntimeError):
    def __init__(self, brand_key: str) -> None:
        super().__init__(f"central score row has unknown brand_key: {brand_key}")
        self.brand_key = brand_key


def density_bucket(evidence_count: int) -> DensityBucket:
    """Classify an Agent2 brand by distinct central-eligible news count."""

    if evidence_count >= FULL_MIN_EVIDENCE:
        return DensityBucket("full", ProcessingMode.LLM_FULL, FULL_MIN_EVIDENCE, None)
    if evidence_count >= MID_MIN_EVIDENCE:
        return DensityBucket(
            "mid",
            ProcessingMode.LLM_COMPACT,
            MID_MIN_EVIDENCE,
            FULL_MIN_EVIDENCE - 1,
        )
    if evidence_count >= SPARSE_MIN_EVIDENCE:
        return DensityBucket(
            "sparse",
            ProcessingMode.LLM_RECAP,
            SPARSE_MIN_EVIDENCE,
            MID_MIN_EVIDENCE - 1,
        )
    return DensityBucket("zero", ProcessingMode.TEMPLATE_ZERO, 0, 0)


def route_brand(
    brand: str,
    score_rows: tuple[BrandedScoreRow, ...],
) -> RouteDecision:
    """Route one brand using only the central eligibility predicate."""

    news_ids: set[str] = set()
    processors: set[str] = set()
    for branded_row in score_rows:
        if branded_row.brand_key != brand:
            continue
        score = branded_row.score
        if not is_agent2_eligible(score):
            continue
        news_ids.add(score.news_id)
        if score.source_processor is not None:
            processors.add(score.source_processor)
    return _route_from_evidence(brand, news_ids, processors)


def _route_from_evidence(
    brand: str,
    news_ids: set[str],
    processors: set[str],
) -> RouteDecision:
    bucket = density_bucket(len(news_ids))
    return RouteDecision(
        brand=brand,
        evidence_count=len(news_ids),
        bucket=bucket.bucket,
        mode=bucket.mode,
        included_processors=tuple(
            processor
            for processor in AGENT2_ALLOWED_PROCESSORS
            if processor in processors
        ),
    )


def route_worklist(
    brands: tuple[str, ...],
    score_rows: tuple[BrandedScoreRow, ...],
) -> tuple[RouteDecision, ...]:
    """Route the complete mart universe with central, fail-closed inputs."""

    brand_set = frozenset(brands)
    news_ids_by_brand = {brand: set() for brand in brands}
    processors_by_brand = {brand: set() for brand in brands}
    for branded_row in score_rows:
        if branded_row.brand_key not in brand_set:
            raise UnknownScoreBrandError(branded_row.brand_key)
        score = branded_row.score
        if not is_agent2_eligible(score):
            continue
        news_ids_by_brand[branded_row.brand_key].add(score.news_id)
        if score.source_processor is not None:
            processors_by_brand[branded_row.brand_key].add(score.source_processor)
    return tuple(
        _route_from_evidence(
            brand,
            news_ids_by_brand[brand],
            processors_by_brand[brand],
        )
        for brand in brands
    )
