"""Versioned serving policy for scored news events."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import logging
from types import MappingProxyType
from typing import Final, Mapping


LOGGER = logging.getLogger(__name__)
REV5674_PROCESSOR: Final = "workflow_196_rev5674"
KNOWN_LEGACY_PROCESSORS: Final = frozenset(
    {
        "corpus_v1",
        "cross_match_adapter_v1",
        "tier2_exact_rule_v1",
        "tier2_llm_v1",
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
        "자본/경영": 53,
        "외부/트렌드": 53,
        "공급/생산": 53,
        "신약/R&D": 73,
        "정책/규제": 69,
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


@lru_cache(maxsize=None)
def event_score_policy(source_processor: str | None) -> EventScorePolicy:
    """Resolve the immutable score policy for a persisted processor marker."""

    if source_processor == REV5674_PROCESSOR:
        return REV5674_POLICY
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


def is_cut_b_exposed(*, score: int | float, source_processor: str | None) -> bool:
    """Return whether a score clears the versioned deep-analysis Cut B."""

    return float(score) >= event_score_policy(source_processor).cut_b_threshold
