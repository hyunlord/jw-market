"""Fail-closed Agent2 news eligibility shared by ETL and AI analysis."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Final, Iterable

from .event_score_policy import event_score_policy, is_news_exposed, news_exposure_sql_predicate


AGENT2_DIRECT_PROCESSORS: Final = (
    "workflow_196_optionB",
    "workflow_196_rev5674",
    "tier2_llm_v1",
    "tier2_llm_v2_rev5671",
)
AGENT2_CROSS_PROCESSORS: Final = ("cross_match_adapter_v1",)
AGENT2_ALLOWED_PROCESSORS: Final = AGENT2_DIRECT_PROCESSORS + AGENT2_CROSS_PROCESSORS
AGENT2_ALLOWED_DERIVATIONS: Final = ("llm_direct", "cross_match")


class OrphanNewsError(RuntimeError):
    """Raised when a persisted score has no matching news row."""


@dataclass(frozen=True, slots=True)
class Agent2ScoreRow:
    """The score and joined-news fields required by the eligibility policy."""

    news_id: str
    source_processor: str | None
    derivation: str | None
    tag: str | None
    score: int | float | Decimal
    published_date: date | datetime | None
    news_exists: bool


@dataclass(frozen=True, slots=True)
class EligibleAgent2Event:
    """An event admitted only through :func:`eligible_agent2_events`."""

    news_id: str
    source_processor: str
    derivation: str
    tag: str
    score: Decimal
    published_date: date | None
    eligibility_revision: str


def _decimal_score(value: int | float | Decimal) -> Decimal:
    try:
        score = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid Agent2 score: {value!r}") from exc
    if not score.is_finite():
        raise ValueError(f"non-finite Agent2 score: {value!r}")
    return score


def _published_date(value: date | datetime | None) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    return value


def is_agent2_eligible(row: Agent2ScoreRow) -> bool:
    """Return central Agent2 eligibility without legacy fallback behavior."""

    if not row.news_id or not row.news_exists:
        raise OrphanNewsError(f"score row has no joined news_raw row: news_id={row.news_id!r}")
    if row.source_processor not in AGENT2_ALLOWED_PROCESSORS:
        return False
    if row.derivation not in AGENT2_ALLOWED_DERIVATIONS:
        return False
    try:
        score = _decimal_score(row.score)
    except ValueError:
        return False
    return is_news_exposed(
        tag=row.tag,
        score=score,
        source_processor=row.source_processor,
    )


def eligible_agent2_events(rows: Iterable[Agent2ScoreRow]) -> tuple[EligibleAgent2Event, ...]:
    """Project eligible rows while preserving row evidence for later selection."""

    events: list[EligibleAgent2Event] = []
    for row in rows:
        if not is_agent2_eligible(row):
            continue
        events.append(
            EligibleAgent2Event(
                news_id=row.news_id,
                source_processor=str(row.source_processor),
                derivation=str(row.derivation),
                tag=str(row.tag),
                score=_decimal_score(row.score),
                published_date=_published_date(row.published_date),
                eligibility_revision=AGENT2_ELIGIBILITY_REVISION,
            )
        )
    return tuple(events)


def eligible_agent2_news_ids(events: Iterable[EligibleAgent2Event]) -> frozenset[str]:
    """Return the contract identity: distinct eligible ``news_id`` values."""

    return frozenset(event.news_id for event in events)


def agent2_eligibility_sql_predicate(
    score_alias: str = "s",
    news_alias: str = "n",
) -> tuple[str, tuple[object, ...]]:
    """Return the SQL predicate equivalent of :func:`is_agent2_eligible`.

    Callers group with ``DISTINCT news_id``. The joined-news clause rejects
    orphans in SQL; a separate orphan census remains a hard gate so they are
    not silently filtered from a run.
    """

    for alias in (score_alias, news_alias):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", alias):
            raise ValueError(f"unsafe SQL alias: {alias!r}")
    exposure_sql, exposure_params = news_exposure_sql_predicate(score_alias)
    processor_marks = ", ".join("%s" for _ in AGENT2_ALLOWED_PROCESSORS)
    derivation_marks = ", ".join("%s" for _ in AGENT2_ALLOWED_DERIVATIONS)
    sql = (
        f"{news_alias}.news_id IS NOT NULL "
        f"AND {score_alias}.source_processor IN ({processor_marks}) "
        f"AND {score_alias}.derivation IN ({derivation_marks}) "
        f"AND ({exposure_sql})"
    )
    params: tuple[object, ...] = (
        *AGENT2_ALLOWED_PROCESSORS,
        *AGENT2_ALLOWED_DERIVATIONS,
        *exposure_params,
    )
    return sql, params


def agent2_eligibility_revision_payload() -> dict[str, object]:
    """Return canonical policy content included in the eligibility revision."""

    processor_policies = {
        processor: {
            "category_cutoffs": dict(event_score_policy(processor).category_cutoffs),
        }
        for processor in sorted(AGENT2_ALLOWED_PROCESSORS)
    }
    return {
        "schema": "agent2-eligibility/v1",
        "allowed_processors": sorted(AGENT2_ALLOWED_PROCESSORS),
        "allowed_derivations": sorted(AGENT2_ALLOWED_DERIVATIONS),
        "excluded_processors": ["tier2_exact_rule_v1"],
        "excluded_tags": ["기타"],
        "unrecognized_category": "reject",
        "orphan_news": "hard_fail",
        "identity": "distinct_news_id",
        "processor_policies": processor_policies,
    }


def _revision(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


AGENT2_ELIGIBILITY_REVISION: Final = _revision(agent2_eligibility_revision_payload())
