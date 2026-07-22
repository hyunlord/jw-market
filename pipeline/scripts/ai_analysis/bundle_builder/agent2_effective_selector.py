"""Pure effective-evidence selector for future Agent2 consumers."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
import calendar
import hashlib
import json
from typing import Final, Iterable

from pipeline.etl.io.mart.agent2_eligibility import (
    AGENT2_ALLOWED_DERIVATIONS,
    AGENT2_ELIGIBILITY_REVISION,
    Agent2ScoreRow,
    EligibleAgent2Event,
    is_agent2_eligible,
)


@dataclass(frozen=True, slots=True)
class EffectiveSelectorConfig:
    lookback_months: int = 6
    direct_prefetch: int = 90
    direct_cap: int = 30
    cross_cap: int = 5
    deduplicate_direct_by_date: bool = True

    def __post_init__(self) -> None:
        if self.lookback_months < 1:
            raise ValueError("lookback_months must be positive")
        if self.direct_prefetch < 1:
            raise ValueError("direct_prefetch must be positive")
        if self.direct_cap < 1 or self.direct_cap > self.direct_prefetch:
            raise ValueError("direct_cap must be positive and no greater than direct_prefetch")
        if self.cross_cap < 1:
            raise ValueError("cross_cap must be positive")


@dataclass(frozen=True, slots=True)
class EffectiveAgent2Selection:
    selected_news_ids: tuple[str, ...]
    selected_direct_news_ids: tuple[str, ...]
    selected_cross_news_ids: tuple[str, ...]
    rejected_by_cap_news_ids: tuple[str, ...]
    rejected_by_dedup_news_ids: tuple[str, ...]
    rejected_outside_lookback_news_ids: tuple[str, ...]
    duplicate_news_ids: tuple[str, ...]
    selector_revision: str


DEFAULT_EFFECTIVE_SELECTOR_CONFIG: Final = EffectiveSelectorConfig()


def _subtract_months(value: date, months: int) -> date:
    target_month_index = value.year * 12 + value.month - 1 - months
    year, zero_based_month = divmod(target_month_index, 12)
    month = zero_based_month + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _event_order(event: EligibleAgent2Event) -> tuple[Decimal, int, str, int, str, str]:
    published_ordinal = (event.published_date or date.min).toordinal()
    derivation_rank = 0 if event.derivation == "llm_direct" else 1
    return (
        -event.score,
        -published_ordinal,
        event.news_id,
        derivation_rank,
        event.source_processor,
        event.tag,
    )


def _distinct_events(
    events: Iterable[EligibleAgent2Event],
) -> tuple[tuple[EligibleAgent2Event, ...], tuple[str, ...]]:
    unique: dict[str, EligibleAgent2Event] = {}
    duplicate_ids: set[str] = set()
    for event in sorted(events, key=_event_order):
        if event.news_id in unique:
            duplicate_ids.add(event.news_id)
            continue
        unique[event.news_id] = event
    return tuple(unique.values()), tuple(sorted(duplicate_ids))


def selector_revision_for_config(config: EffectiveSelectorConfig) -> str:
    payload = {
        "schema": "agent2-effective-selector/v1",
        "config": asdict(config),
        "input": "central_eligible_rows_only",
        "input_eligibility_revision": AGENT2_ELIGIBILITY_REVISION,
        "identity": "distinct_news_id",
        "ordering": ["score_desc", "published_date_desc", "news_id_asc"],
        "direct_dedup_key": "published_date",
        "date_window": "inclusive_mysql_date_sub_months",
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


DEFAULT_EFFECTIVE_SELECTOR_REVISION: Final = selector_revision_for_config(
    DEFAULT_EFFECTIVE_SELECTOR_CONFIG
)


def select_effective_agent2_events(
    events: Iterable[EligibleAgent2Event],
    *,
    snapshot_date: date,
    config: EffectiveSelectorConfig = DEFAULT_EFFECTIVE_SELECTOR_CONFIG,
) -> EffectiveAgent2Selection:
    """Apply lookback, deterministic deduplication, ordering, and caps."""

    materialized = tuple(events)
    for event in materialized:
        if not isinstance(event, EligibleAgent2Event):
            raise TypeError("effective selector accepts EligibleAgent2Event inputs only")
        if event.eligibility_revision != AGENT2_ELIGIBILITY_REVISION:
            raise ValueError(
                "effective selector input eligibility revision does not match the central policy"
            )
        if event.derivation not in AGENT2_ALLOWED_DERIVATIONS:
            raise ValueError(f"non-central derivation: {event.derivation!r}")
        if not is_agent2_eligible(
            Agent2ScoreRow(
                news_id=event.news_id,
                source_processor=event.source_processor,
                derivation=event.derivation,
                tag=event.tag,
                score=event.score,
                published_date=event.published_date,
                news_exists=True,
            )
        ):
            raise ValueError(f"event does not satisfy central eligibility: {event.news_id!r}")

    distinct, duplicate_ids = _distinct_events(materialized)
    window_start = _subtract_months(snapshot_date, config.lookback_months)
    in_window: list[EligibleAgent2Event] = []
    outside_ids: list[str] = []
    for event in distinct:
        if event.published_date is None or not (window_start <= event.published_date <= snapshot_date):
            outside_ids.append(event.news_id)
        else:
            in_window.append(event)

    direct = sorted(
        (event for event in in_window if event.derivation == "llm_direct"),
        key=_event_order,
    )
    cross = sorted(
        (event for event in in_window if event.derivation == "cross_match"),
        key=_event_order,
    )

    prefetched_direct = direct[: config.direct_prefetch]
    cap_rejected: list[str] = [event.news_id for event in direct[config.direct_prefetch :]]
    dedup_rejected: list[str] = []
    if config.deduplicate_direct_by_date:
        by_date: dict[date, EligibleAgent2Event] = {}
        for event in prefetched_direct:
            event_date = event.published_date
            if event_date in by_date:
                dedup_rejected.append(event.news_id)
                continue
            if event_date is not None:
                by_date[event_date] = event
        deduplicated_direct = list(by_date.values())
    else:
        deduplicated_direct = prefetched_direct

    selected_direct = deduplicated_direct[: config.direct_cap]
    cap_rejected.extend(event.news_id for event in deduplicated_direct[config.direct_cap :])
    selected_cross = cross[: config.cross_cap]
    cap_rejected.extend(event.news_id for event in cross[config.cross_cap :])

    selected_direct_ids = tuple(event.news_id for event in selected_direct)
    selected_cross_ids = tuple(event.news_id for event in selected_cross)
    return EffectiveAgent2Selection(
        selected_news_ids=tuple(sorted(set(selected_direct_ids) | set(selected_cross_ids))),
        selected_direct_news_ids=selected_direct_ids,
        selected_cross_news_ids=selected_cross_ids,
        rejected_by_cap_news_ids=tuple(sorted(set(cap_rejected))),
        rejected_by_dedup_news_ids=tuple(sorted(set(dedup_rejected))),
        rejected_outside_lookback_news_ids=tuple(sorted(set(outside_ids))),
        duplicate_news_ids=duplicate_ids,
        selector_revision=selector_revision_for_config(config),
    )
