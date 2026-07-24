"""Pure legacy-bundle versus central-selector shadow comparison."""

from __future__ import annotations

import calendar
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Final, Iterable

from pipeline.etl.io.mart.agent2_eligibility import (
    AGENT2_ELIGIBILITY_REVISION,
    Agent2ScoreRow,
    OrphanNewsError,
    eligible_agent2_events,
)

from .agent2_effective_selector import (
    DEFAULT_EFFECTIVE_SELECTOR_CONFIG,
    EffectiveSelectorConfig,
    select_effective_agent2_events,
)
from .event_bundle_builder import (
    CROSS_MATCH_SOURCE_PROCESSORS,
    DIRECT_EVENT_SOURCE_PROCESSORS,
)

LEGACY_BUNDLE_SCORE_CUTOFF: Final = 50


@dataclass(frozen=True, slots=True)
class BundleShadowDecision:
    brand_key: str
    bundle_news_ids: tuple[str, ...]
    bundle_direct_news_ids: tuple[str, ...]
    bundle_cross_news_ids: tuple[str, ...]
    central_news_ids: tuple[str, ...]
    central_direct_news_ids: tuple[str, ...]
    central_cross_news_ids: tuple[str, ...]
    central_orphan_news_ids: tuple[str, ...]
    matches: bool
    legacy_revision: str
    selector_revision: str


@dataclass(frozen=True, slots=True)
class _LegacyRow:
    news_id: str
    derivation: str
    score: Decimal
    published_date: date


def _subtract_months(value: date, months: int) -> date:
    target_month_index = value.year * 12 + value.month - 1 - months
    year, zero_based_month = divmod(target_month_index, 12)
    month = zero_based_month + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _published_date(value: date | datetime | None) -> date | None:
    return value.date() if isinstance(value, datetime) else value


def _score(value: int | float | Decimal) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _legacy_order(row: _LegacyRow) -> tuple[Decimal, int, str]:
    return (-row.score, -row.published_date.toordinal(), row.news_id)


def _legacy_rows(
    rows: Iterable[Agent2ScoreRow],
    *,
    snapshot_date: date,
    config: EffectiveSelectorConfig,
) -> tuple[_LegacyRow, ...]:
    window_start = _subtract_months(snapshot_date, config.lookback_months)
    selected: list[_LegacyRow] = []
    for row in rows:
        published = _published_date(row.published_date)
        score = _score(row.score)
        allowed_processor = (
            row.derivation == "llm_direct"
            and row.source_processor in DIRECT_EVENT_SOURCE_PROCESSORS
        ) or (
            row.derivation == "cross_match"
            and row.source_processor in CROSS_MATCH_SOURCE_PROCESSORS
        )
        if (
            not row.news_id
            or not row.news_exists
            or not allowed_processor
            or row.tag in {None, "기타"}
            or score is None
            or score < LEGACY_BUNDLE_SCORE_CUTOFF
            or published is None
            or not window_start <= published <= snapshot_date
        ):
            continue
        selected.append(
            _LegacyRow(
                news_id=row.news_id,
                derivation=str(row.derivation),
                score=score,
                published_date=published,
            )
        )
    return tuple(sorted(selected, key=_legacy_order))


def _legacy_select(
    rows: Iterable[Agent2ScoreRow],
    *,
    snapshot_date: date,
    config: EffectiveSelectorConfig,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    admitted = _legacy_rows(rows, snapshot_date=snapshot_date, config=config)
    prefetched = [
        row for row in admitted if row.derivation == "llm_direct"
    ][: config.direct_prefetch]
    if config.deduplicate_direct_by_date:
        by_date: dict[date, _LegacyRow] = {}
        for row in prefetched:
            existing = by_date.get(row.published_date)
            if existing is None or (row.score, row.news_id) > (
                existing.score,
                existing.news_id,
            ):
                by_date[row.published_date] = row
        direct = sorted(
            by_date.values(),
            key=lambda row: (-row.score, row.published_date, row.news_id),
        )
    else:
        direct = prefetched
    cross = [row for row in admitted if row.derivation == "cross_match"]
    direct_ids = tuple(sorted({row.news_id for row in direct[: config.direct_cap]}))
    cross_ids = tuple(sorted({row.news_id for row in cross[: config.cross_cap]}))
    return direct_ids, cross_ids


def _legacy_revision(config: EffectiveSelectorConfig) -> str:
    payload = {
        "schema": "agent2-bundle-shadow-legacy/v1",
        "score_cutoff": LEGACY_BUNDLE_SCORE_CUTOFF,
        "direct_processors": DIRECT_EVENT_SOURCE_PROCESSORS,
        "cross_processors": CROSS_MATCH_SOURCE_PROCESSORS,
        "config": asdict(config),
        "identity": "selected_distinct_news_id",
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def compare_event_bundle_with_shadow(
    brand_key: str,
    rows: Iterable[Agent2ScoreRow],
    *,
    snapshot_date: date,
    config: EffectiveSelectorConfig = DEFAULT_EFFECTIVE_SELECTOR_CONFIG,
) -> BundleShadowDecision:
    """Return legacy serving selection plus central-selector shadow evidence."""

    materialized = tuple(rows)
    bundle_direct, bundle_cross = _legacy_select(
        materialized,
        snapshot_date=snapshot_date,
        config=config,
    )
    eligible = []
    orphan_ids: set[str] = set()
    for row in materialized:
        try:
            eligible.extend(eligible_agent2_events((row,)))
        except OrphanNewsError:
            orphan_ids.add(row.news_id)
    central = select_effective_agent2_events(
        eligible,
        snapshot_date=snapshot_date,
        config=config,
    )
    bundle_ids = tuple(sorted(set(bundle_direct) | set(bundle_cross)))
    central_ids = central.selected_news_ids
    return BundleShadowDecision(
        brand_key=brand_key,
        bundle_news_ids=bundle_ids,
        bundle_direct_news_ids=bundle_direct,
        bundle_cross_news_ids=bundle_cross,
        central_news_ids=central_ids,
        central_direct_news_ids=tuple(sorted(central.selected_direct_news_ids)),
        central_cross_news_ids=tuple(sorted(central.selected_cross_news_ids)),
        central_orphan_news_ids=tuple(sorted(orphan_ids)),
        matches=bundle_ids == central_ids,
        legacy_revision=_legacy_revision(config),
        selector_revision=central.selector_revision,
    )
