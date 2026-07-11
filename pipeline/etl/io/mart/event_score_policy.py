"""Versioned serving policy for scored news events."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import logging
import re
from types import MappingProxyType
from typing import Final, Mapping


LOGGER = logging.getLogger(__name__)
REV5674_PROCESSOR: Final = "workflow_196_rev5674"
TIER2_LLM_V1_PROCESSOR: Final = "tier2_llm_v1"
TIER2_LLM_V2_PROCESSOR: Final = "tier2_llm_v2_rev5671"
KNOWN_LEGACY_PROCESSORS: Final = frozenset(
    {
        "corpus_v1",
        "cross_match_adapter_v1",
        "tier2_exact_rule_v1",
        "workflow_196_optionB",
    }
)
LEGACY_CATEGORY_CUTOFFS: Final = MappingProxyType(
    {
        "자본/경영": 43,
        "외부/트렌드": 49,
        "공급/생산": 51,
        "신약/R&D": 54,
        "정책/규제": 55,
    }
)
REV5674_CATEGORY_CUTOFFS: Final = MappingProxyType(
    {
        "자본/경영": 43,
        "외부/트렌드": 48,
        "공급/생산": 43,
        "신약/R&D": 58,
        "정책/규제": 54,
    }
)
TIER2_CATEGORY_CUTOFFS: Final = MappingProxyType(
    {
        "자본/경영": 41,
        "외부/트렌드": 48,
        "공급/생산": 22,
        "신약/R&D": 62,
        "정책/규제": 58,
    }
)


@dataclass(frozen=True, slots=True)
class EventScorePolicy:
    """News-list and Cut B thresholds for one processor generation."""

    category_cutoffs: Mapping[str, int]
    cut_b_threshold: int


LEGACY_POLICY: Final = EventScorePolicy(
    category_cutoffs=LEGACY_CATEGORY_CUTOFFS,
    cut_b_threshold=80,
)
REV5674_POLICY: Final = EventScorePolicy(
    category_cutoffs=REV5674_CATEGORY_CUTOFFS,
    cut_b_threshold=88,
)
TIER2_POLICY: Final = EventScorePolicy(
    category_cutoffs=TIER2_CATEGORY_CUTOFFS,
    cut_b_threshold=88,
)


@lru_cache(maxsize=None)
def event_score_policy(source_processor: str | None) -> EventScorePolicy:
    """Resolve the immutable score policy for a persisted processor marker."""

    if source_processor == REV5674_PROCESSOR:
        return REV5674_POLICY
    if source_processor in (TIER2_LLM_V1_PROCESSOR, TIER2_LLM_V2_PROCESSOR):
        return TIER2_POLICY
    if source_processor and source_processor not in KNOWN_LEGACY_PROCESSORS:
        LOGGER.warning(
            "Unknown event score processor %r; applying legacy exposure policy",
            source_processor,
        )
    return LEGACY_POLICY


def is_news_exposed(*, tag: str | None, score: int | float, source_processor: str | None) -> bool:
    """Return whether a score clears its versioned category cutoff."""

    policy = event_score_policy(source_processor)
    cutoff = policy.category_cutoffs.get(str(tag or ""))
    return cutoff is not None and float(score) >= cutoff


def news_exposure_sql_predicate(table_alias: str = "s") -> tuple[str, tuple[object, ...]]:
    """Return a SQL predicate matching :func:`is_news_exposed`.

    The cache/event SQL paths use this instead of re-declaring cutoff literals so
    the processor-versioned policy remains single-sourced with the Python
    fallback checks.
    """

    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table_alias):
        raise ValueError(f"unsafe SQL alias: {table_alias!r}")
    tag_col = f"{table_alias}.tag"
    score_col = f"{table_alias}.score"
    processor_col = f"{table_alias}.source_processor"

    def category_clause(cutoffs: Mapping[str, int]) -> tuple[str, list[object]]:
        clauses: list[str] = []
        params: list[object] = []
        for tag, cutoff in cutoffs.items():
            clauses.append(f"({tag_col} = %s AND {score_col} >= %s)")
            params.extend([tag, cutoff])
        return " OR ".join(clauses), params

    legacy_clause, legacy_params = category_clause(LEGACY_CATEGORY_CUTOFFS)
    rev_clause, rev_params = category_clause(REV5674_CATEGORY_CUTOFFS)
    tier2_clause, tier2_params = category_clause(TIER2_CATEGORY_CUTOFFS)
    sql = (
        f"{tag_col} <> %s AND ("
        f"({processor_col} = %s AND ({rev_clause})) OR "
        f"({processor_col} IN (%s, %s) AND ({tier2_clause})) OR "
        f"(({processor_col} IS NULL OR "
        f"({processor_col} <> %s AND {processor_col} <> %s AND {processor_col} <> %s)) "
        f"AND ({legacy_clause}))"
        ")"
    )
    params: list[object] = [
        "기타",
        REV5674_PROCESSOR,
        *rev_params,
        TIER2_LLM_V1_PROCESSOR,
        TIER2_LLM_V2_PROCESSOR,
        *tier2_params,
        REV5674_PROCESSOR,
        TIER2_LLM_V1_PROCESSOR,
        TIER2_LLM_V2_PROCESSOR,
        *legacy_params,
    ]
    return sql, tuple(params)


def is_cut_b_exposed(*, score: int | float, source_processor: str | None) -> bool:
    """Return whether a score clears the versioned deep-analysis Cut B."""

    return float(score) >= event_score_policy(source_processor).cut_b_threshold
