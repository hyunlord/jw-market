"""Deterministic post-crawl selection and receipts for the Agent2 hook."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final, Iterable, Literal

from pipeline.etl.io.mart.agent2_eligibility import (
    AGENT2_ELIGIBILITY_REVISION,
    EligibleAgent2Event,
    eligible_agent2_events,
)
from pipeline.scripts.ai_analysis.agent2_density_worklist import (
    build_brand_identities,
    build_central_evidence_from_rows,
)
from pipeline.scripts.ai_analysis.bundle_builder.agent2_effective_selector import (
    DEFAULT_EFFECTIVE_SELECTOR_CONFIG,
    EffectiveSelectorConfig,
    select_effective_agent2_events,
)
WF217_TIMEOUT_SECONDS: Final = 240
WF217_MAX_ATTEMPTS: Final = 2
WF217_BACKOFF_SECONDS: Final = 5
DEFAULT_ESTIMATED_USD_PER_CALL: Final = Decimal("0.0439")


@dataclass(frozen=True, slots=True)
class Agent2HookEvidence:
    news_id: str
    published_date: date | None
    score: str
    tag: str
    source_processor: str
    derivation: str


@dataclass(frozen=True, slots=True)
class Agent2HookTarget:
    brand_key: str
    canonical_brand_name: str
    selected_news_ids: tuple[str, ...]
    effective_added_news_ids: tuple[str, ...]
    evidence: tuple[Agent2HookEvidence, ...]


@dataclass(frozen=True, slots=True)
class Agent2DetectionResult:
    snapshot_date: date
    targets: tuple[Agent2HookTarget, ...]
    eligibility_revision: str
    selector_revision: str
    registry_revision: str

    @property
    def target_count(self) -> int:
        return len(self.targets)


@dataclass(frozen=True, slots=True)
class Agent2HookCost:
    estimated_usd_per_call: Decimal

    def __post_init__(self) -> None:
        if self.estimated_usd_per_call < 0:
            raise ValueError("estimated_usd_per_call must be non-negative")


Agent2ExecutionMode = Literal[
    "no_targets",
    "selection_only",
    "blocked_by_limit",
    "wf217_enabled",
]


@dataclass(frozen=True, slots=True)
class Agent2GenerationPlan:
    target_count: int
    expected_llm_calls: int
    allowed_llm_calls: int
    estimated_cost_usd: Decimal
    worst_case_seconds: int
    execution_mode: Agent2ExecutionMode


def _baseline_eligible_news_ids(
    baseline_news_ids_by_brand: dict[str, frozenset[str]],
) -> frozenset[str]:
    return frozenset(
        news_id
        for news_ids in baseline_news_ids_by_brand.values()
        for news_id in news_ids
    )


def _evidence_by_news_id(
    events: Iterable[EligibleAgent2Event],
) -> dict[str, Agent2HookEvidence]:
    return {
        event.news_id: Agent2HookEvidence(
            news_id=event.news_id,
            published_date=event.published_date,
            score=str(event.score),
            tag=event.tag,
            source_processor=event.source_processor,
            derivation=event.derivation,
        )
        for event in events
    }


def detect_increased_brands_from_rows(
    *,
    brand_rows: list[dict[str, object]],
    score_rows: list[dict[str, object]],
    baseline_news_ids_by_brand: dict[str, frozenset[str]],
    snapshot_date: date,
    lookback_months: int = DEFAULT_EFFECTIVE_SELECTOR_CONFIG.lookback_months,
    direct_cap: int = DEFAULT_EFFECTIVE_SELECTOR_CONFIG.direct_cap,
    cross_cap: int = DEFAULT_EFFECTIVE_SELECTOR_CONFIG.cross_cap,
) -> Agent2DetectionResult:
    """Return brands whose effective evidence gained at least one news ID."""

    identities = build_brand_identities(brand_rows)
    names_by_key = {
        identity.brand_key: identity.canonical_brand_name for identity in identities
    }
    baseline_news_ids = _baseline_eligible_news_ids(
        baseline_news_ids_by_brand
    )
    central = build_central_evidence_from_rows(brand_rows, score_rows)
    grouped: dict[str, list[EligibleAgent2Event]] = defaultdict(list)
    for branded in central.score_rows:
        grouped[branded.brand_key].extend(eligible_agent2_events((branded.score,)))

    selector_config = EffectiveSelectorConfig(
        lookback_months=lookback_months,
        direct_prefetch=max(direct_cap * 3, direct_cap),
        direct_cap=direct_cap,
        cross_cap=cross_cap,
        deduplicate_direct_by_date=True,
    )
    targets: list[Agent2HookTarget] = []
    selector_revision = ""
    for brand_key in sorted(grouped):
        events = tuple(grouped[brand_key])
        selection = select_effective_agent2_events(
            events,
            snapshot_date=snapshot_date,
            config=selector_config,
        )
        selector_revision = selection.selector_revision
        canonical_name = names_by_key[brand_key]
        added = tuple(
            sorted(
                set(selection.selected_news_ids)
                - baseline_news_ids
            )
        )
        if not added:
            continue
        evidence_by_id = _evidence_by_news_id(events)
        targets.append(
            Agent2HookTarget(
                brand_key=brand_key,
                canonical_brand_name=canonical_name,
                selected_news_ids=selection.selected_news_ids,
                effective_added_news_ids=added,
                evidence=tuple(evidence_by_id[news_id] for news_id in added),
            )
        )
    if not selector_revision:
        selector_revision = select_effective_agent2_events(
            (),
            snapshot_date=snapshot_date,
            config=selector_config,
        ).selector_revision
    return Agent2DetectionResult(
        snapshot_date=snapshot_date,
        targets=tuple(targets),
        eligibility_revision=AGENT2_ELIGIBILITY_REVISION,
        selector_revision=selector_revision,
        registry_revision=central.registry_revision,
    )


def build_agent2_generation_plan(
    result: Agent2DetectionResult,
    *,
    llm_call_limit: int,
    cost: Agent2HookCost,
) -> Agent2GenerationPlan:
    """Size wf217 work without invoking the workflow."""

    if llm_call_limit < 0:
        raise ValueError("llm_call_limit must be non-negative")
    expected = result.target_count
    if expected == 0:
        mode: Agent2ExecutionMode = "no_targets"
        allowed = 0
    elif llm_call_limit == 0:
        mode = "selection_only"
        allowed = 0
    elif expected > llm_call_limit:
        mode = "blocked_by_limit"
        allowed = 0
    else:
        mode = "wf217_enabled"
        allowed = expected
    return Agent2GenerationPlan(
        target_count=expected,
        expected_llm_calls=expected,
        allowed_llm_calls=allowed,
        estimated_cost_usd=cost.estimated_usd_per_call * expected,
        worst_case_seconds=expected
        * (WF217_TIMEOUT_SECONDS * WF217_MAX_ATTEMPTS + WF217_BACKOFF_SECONDS),
        execution_mode=mode,
    )
