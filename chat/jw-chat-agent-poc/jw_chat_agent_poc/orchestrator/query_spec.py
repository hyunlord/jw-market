from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Protocol, TypedDict

from jw_chat_agent_poc.agent_loop.periods import AgentPeriodGrounding
from jw_chat_agent_poc.resolver import BrandResolution


LOGGER = logging.getLogger(__name__)
V3_TOOL_SELECTION_SHADOW_ENV = "JW_CHAT_V3_TOOL_SELECTION_SHADOW"


class EntityKind(StrEnum):
    BRAND = "brand"
    MARKET = "market"


class QueryOperation(StrEnum):
    CURRENT_VALUE = "current_value"
    TIME_SERIES = "time_series"
    PERIOD_AGGREGATE = "period_aggregate"
    COMPARE_CURRENT = "compare_current"
    COMPARE_CHANGE = "compare_change"
    RANK = "rank"
    DATE_RANGE_BOUNDARY = "date_range_boundary"


class TimeGranularity(StrEnum):
    MONTH = "month"
    QUARTER = "quarter"
    YEAR = "year"


class QueryFacet(StrEnum):
    FILE = "file"
    MARKET = "market"
    CHANNEL = "channel"
    FORECAST = "forecast"
    COMPETITOR_ACTIVITY = "competitor_activity"
    COMPETITION_TREND = "competition_trend"
    GROWTH_DRIVER = "growth_driver"


@dataclass(frozen=True, slots=True)
class QueryEntity:
    kind: EntityKind
    canonical_id: str
    display_name: str


@dataclass(frozen=True, slots=True)
class RequestQuerySpec:
    entities: tuple[QueryEntity, ...]
    operation: QueryOperation
    metrics: tuple[str, ...]
    start_period: str | None = None
    end_period: str | None = None
    window_count: int | None = None
    granularity: TimeGranularity | None = None
    comparison_targets: tuple[QueryEntity, ...] = ()
    source: str | None = None
    requested_view: str | None = None
    facets: tuple[QueryFacet, ...] = ()


class QueryEntityResolver(Protocol):
    def resolve_many(
        self,
        question_or_brands: str,
        allow_default: bool = False,
    ) -> tuple[BrandResolution, ...]: ...


class ObservedEntity(TypedDict):
    kind: str
    canonical_id: str
    display_name: str


class QuerySpecObservation(TypedDict):
    entities: list[ObservedEntity]
    operation: str
    metrics: list[str]
    start_period: str | None
    end_period: str | None
    window_count: int | None
    granularity: str | None
    comparison_targets: list[ObservedEntity]
    source: str | None
    requested_view: str | None
    facets: list[str]


_WINDOW_RE = re.compile(r"최근\s*(\d+)\s*(?:개\s*)?(개월|달|분기|개년|년)")


def extract_query_spec(
    question: str,
    resolver: QueryEntityResolver,
    grounding: AgentPeriodGrounding,
) -> RequestQuerySpec:
    _observe_v3_tool_selection_shadow(question)
    entities = _entities(question, resolver)
    metrics = _metrics(question)
    window_count, granularity = _window(question)
    operation = _operation(question, entities, window_count)
    start_period, end_period = _period_bounds(operation, grounding)
    comparison_targets = (
        entities
        if operation in {QueryOperation.COMPARE_CURRENT, QueryOperation.COMPARE_CHANGE}
        else ()
    )
    return RequestQuerySpec(
        entities=entities,
        operation=operation,
        metrics=metrics,
        start_period=start_period,
        end_period=end_period,
        window_count=window_count,
        granularity=granularity,
        comparison_targets=comparison_targets,
        source=_source(question),
        requested_view=_requested_view(question),
        facets=_facets(question),
    )


def _observe_v3_tool_selection_shadow(question: str) -> None:
    if os.environ.get(V3_TOOL_SELECTION_SHADOW_ENV, "").strip().lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return
    try:
        from jw_chat_agent_poc.orchestrator.shadow_gate_runtime import (
            current_shadow_request_id,
        )
        from jw_chat_agent_poc.tool_use.v3_selection_shadow import (
            start_v3_selection_shadow,
        )

        start_v3_selection_shadow(
            question,
            request_id=current_shadow_request_id(),
        )
    except Exception:  # noqa: BLE001 - V3 observation cannot affect the served answer
        LOGGER.exception("v3_tool_selection_shadow_start_failed")


def query_spec_observation(spec: RequestQuerySpec) -> QuerySpecObservation:
    return {
        "entities": [_observed_entity(entity) for entity in spec.entities],
        "operation": spec.operation.value,
        "metrics": list(spec.metrics),
        "start_period": spec.start_period,
        "end_period": spec.end_period,
        "window_count": spec.window_count,
        "granularity": spec.granularity.value if spec.granularity is not None else None,
        "comparison_targets": [
            _observed_entity(entity) for entity in spec.comparison_targets
        ],
        "source": spec.source,
        "requested_view": spec.requested_view,
        "facets": [facet.value for facet in spec.facets],
    }


def with_resolved_entities(
    spec: RequestQuerySpec,
    resolutions: tuple[BrandResolution, ...],
) -> RequestQuerySpec:
    """Reconcile the request contract with the resolver used by execution.

    The service preflight and the agent can have different catalog readers. The
    execution resolver can add entities that preflight missed, while preflight
    entities must not disappear before plan coverage is evaluated.
    """

    runtime_entities = tuple(
        QueryEntity(
            kind=EntityKind.BRAND,
            canonical_id=resolution.canonical_brand,
            display_name=resolution.canonical_brand,
        )
        for resolution in resolutions
    )
    entities = tuple(
        dict.fromkeys((*spec.entities, *runtime_entities))
    )
    if entities == spec.entities:
        return spec
    comparison_targets = (
        entities
        if spec.operation in {QueryOperation.COMPARE_CURRENT, QueryOperation.COMPARE_CHANGE}
        else ()
    )
    return replace(
        spec,
        entities=entities,
        comparison_targets=comparison_targets,
    )


def _entities(
    question: str,
    resolver: QueryEntityResolver,
) -> tuple[QueryEntity, ...]:
    try:
        resolutions = resolver.resolve_many(question, allow_default=False)
    except (LookupError, OSError, TypeError, ValueError):
        return ()
    return tuple(
        QueryEntity(
            kind=EntityKind.BRAND,
            canonical_id=resolution.canonical_brand,
            display_name=resolution.canonical_brand,
        )
        for resolution in resolutions
    )


def _metrics(question: str) -> tuple[str, ...]:
    candidates = (
        ("sales", ("매출", "실적", "판매", "팔렸", "팔려")),
        ("share", ("점유율", "MS")),
        ("hhi", ("HHI", "집중도")),
        ("market_size", ("시장 규모", "시장규모")),
        ("rank", ("순위", "랭킹")),
        ("growth", ("성장률",)),
    )
    normalized = question.upper()
    return tuple(
        metric
        for metric, tokens in candidates
        if any(token.upper() in normalized for token in tokens)
    )


def _window(question: str) -> tuple[int | None, TimeGranularity | None]:
    match = _WINDOW_RE.search(question)
    if match is None:
        return None, None
    unit = match.group(2)
    granularity = {
        "개월": TimeGranularity.MONTH,
        "달": TimeGranularity.MONTH,
        "분기": TimeGranularity.QUARTER,
        "개년": TimeGranularity.YEAR,
        "년": TimeGranularity.YEAR,
    }[unit]
    return int(match.group(1)), granularity


def _operation(
    question: str,
    entities: tuple[QueryEntity, ...],
    window_count: int | None,
) -> QueryOperation:
    if "시작일" in question and "종료일" in question:
        return QueryOperation.DATE_RANGE_BOUNDARY
    explicit_comparison = any(
        token in question
        for token in ("비교", "vs", "VS", "중 누가 더", "나란히")
    )
    comparison_request = len(entities) > 1 or bool(entities and explicit_comparison)
    if comparison_request and any(token in question for token in ("변화", "추이", "증감")):
        return QueryOperation.COMPARE_CHANGE
    if comparison_request and any(
        token in question
        for token in (
            "비교",
            "각각",
            "와",
            "과",
            "이랑",
            "vs",
            "VS",
            "중 누가 더",
            "나란히",
        )
    ):
        return QueryOperation.COMPARE_CURRENT
    if window_count is not None or "추이" in question:
        return QueryOperation.TIME_SERIES
    if any(token in question for token in ("합계", "누적", "총합")):
        return QueryOperation.PERIOD_AGGREGATE
    if any(token in question for token in ("순위", "랭킹")):
        return QueryOperation.RANK
    return QueryOperation.CURRENT_VALUE


def _period_bounds(
    operation: QueryOperation,
    grounding: AgentPeriodGrounding,
) -> tuple[str | None, str | None]:
    if operation is QueryOperation.DATE_RANGE_BOUNDARY:
        return grounding.first_period, grounding.latest_period
    ignored = {"latest", "previous_year"}
    if "latest" in grounding.pre_resolved_periods:
        ignored.add(grounding.latest_period)
    explicit = tuple(
        period
        for period in grounding.pre_resolved_periods
        if period not in ignored
    )
    if not explicit:
        return None, None
    return explicit[0], explicit[-1]


def _source(question: str) -> str | None:
    normalized = question.upper()
    for source in ("UBIST", "IQVIA", "HIRA", "MFDS"):
        if source in normalized:
            return source.lower()
    return None


def _requested_view(question: str) -> str | None:
    if "일반뷰" in question:
        return "general_view"
    if "전략뷰" in question:
        return "market_landscape"
    if "경쟁뷰" in question:
        return "competitive_dynamics"
    return None


def _facets(question: str) -> tuple[QueryFacet, ...]:
    normalized = question.casefold()
    requested = (
        (QueryFacet.FILE, any(token in normalized for token in ("파일", "문서", "보고서"))),
        (
            QueryFacet.MARKET,
            any(
                token in normalized
                for token in ("시스템 데이터", "시장 데이터", "mart", "db")
            ),
        ),
        (QueryFacet.CHANNEL, any(token in normalized for token in ("채널", "진료과"))),
        (
            QueryFacet.FORECAST,
            any(token in normalized for token in ("앞으로", "전망", "예측")),
        ),
        (
            QueryFacet.COMPETITOR_ACTIVITY,
            "경쟁" in normalized
            and any(token in normalized for token in ("영업활동", "영업 활동")),
        ),
        (
            QueryFacet.COMPETITION_TREND,
            "경쟁" in normalized
            and any(token in normalized for token in ("변화", "변하", "추이")),
        ),
        (
            QueryFacet.GROWTH_DRIVER,
            any(token in normalized for token in ("성장 원인", "성장원인")),
        ),
    )
    return tuple(facet for facet, present in requested if present)


def _observed_entity(entity: QueryEntity) -> ObservedEntity:
    return {
        "kind": entity.kind.value,
        "canonical_id": entity.canonical_id,
        "display_name": entity.display_name,
    }
