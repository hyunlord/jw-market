from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from jw_chat_agent_poc.agent_loop.periods import build_period_grounding
from jw_chat_agent_poc.orchestrator.query_spec import (
    CANONICAL_METRIC_DEFINITIONS,
    CanonicalMetric,
    EntityKind,
    QueryFacet,
    QueryOperation,
    TimeGranularity,
    extract_query_spec,
    query_spec_observation,
    with_resolved_entities,
)
from jw_chat_agent_poc.resolver import BrandResolution, BrandResolver


class _AcceptanceResolver:
    _BRANDS = ("아일리아", "비오뷰", "리바로")

    def resolve_many(
        self,
        question_or_brands: str,
        allow_default: bool = False,
    ) -> tuple[BrandResolution, ...]:
        del allow_default
        return tuple(
            BrandResolution(
                canonical_brand=brand,
                audit_code=f"test:{brand}",
                molecule_en=(),
                atc=(),
                edi_code=None,
                item_seq=None,
                is_combo=False,
            )
            for brand in self._BRANDS
            if brand in question_or_brands
        )


@pytest.mark.parametrize(
    ("case_id", "question", "expected"),
    (
        ("SB06", "아일리아 매출 알려줘", QueryOperation.CURRENT_VALUE),
        ("PR08", "아일리아 최근 4개 분기 매출 알려줘", QueryOperation.TIME_SERIES),
        ("MB08", "아일리아와 비오뷰 매출 비교해줘", QueryOperation.COMPARE_CURRENT),
        ("CQ06", "아일리아 요새 매출 어때", QueryOperation.CURRENT_VALUE),
        ("SB01", "리바로 매출 알려줘", QueryOperation.CURRENT_VALUE),
        (
            "PR05",
            "리바로 매출 데이터의 시작일과 종료일 알려줘",
            QueryOperation.DATE_RANGE_BOUNDARY,
        ),
        ("AM01", "리바로 매출 알려줘", QueryOperation.CURRENT_VALUE),
        ("ED05", "리바로 매출 매출 매출 알려줘", QueryOperation.CURRENT_VALUE),
    ),
)
def test_acceptance_questions_extract_expected_operation(
    case_id: str,
    question: str,
    expected: QueryOperation,
) -> None:
    resolver = _AcceptanceResolver()

    spec = extract_query_spec(question, resolver, build_period_grounding(question))

    assert spec.operation is expected, case_id
    assert spec.metrics == ("sales",)
    assert spec.entities
    assert all(entity.kind is EntityKind.BRAND for entity in spec.entities)


def test_time_series_and_comparison_details_are_observable() -> None:
    resolver = _AcceptanceResolver()

    time_series = extract_query_spec(
        "아일리아 최근 4개 분기 매출 알려줘",
        resolver,
        build_period_grounding("아일리아 최근 4개 분기 매출 알려줘"),
    )
    comparison = extract_query_spec(
        "아일리아와 비오뷰 매출 비교해줘",
        resolver,
        build_period_grounding("아일리아와 비오뷰 매출 비교해줘"),
    )

    assert time_series.window_count == 4
    assert time_series.granularity is TimeGranularity.QUARTER
    assert time_series.start_period is None
    assert time_series.end_period is None
    assert tuple(entity.display_name for entity in comparison.comparison_targets) == (
        "아일리아",
        "비오뷰",
    )


def test_query_spec_is_immutable_and_observation_omits_raw_question() -> None:
    question = "리바로 매출 알려줘"
    spec = extract_query_spec(
        question,
        _AcceptanceResolver(),
        build_period_grounding(question),
    )

    with pytest.raises(FrozenInstanceError):
        setattr(spec, "operation", QueryOperation.TIME_SERIES)

    observation = query_spec_observation(spec)

    assert observation["operation"] == "current_value"
    assert observation["entities"] == [
        {"kind": "brand", "canonical_id": "리바로", "display_name": "리바로"}
    ]
    assert question not in str(observation)


def test_runtime_resolutions_restore_all_mb10_entities_without_losing_request_shape() -> None:
    question = "리바로, 리바로젯, 로수젯, 리피토 네 브랜드 순위를 비교해줘"
    initial = extract_query_spec(
        question,
        _AcceptanceResolver(),
        build_period_grounding(question),
    )
    runtime = tuple(
        BrandResolution(
            canonical_brand=brand,
            audit_code=f"runtime:{brand}",
            molecule_en=(),
            atc=(),
            edi_code=None,
            item_seq=None,
            is_combo=False,
        )
        for brand in ("리바로", "리바로젯", "로수젯", "리피토")
    )

    reconciled = with_resolved_entities(initial, runtime)

    assert tuple(entity.canonical_id for entity in initial.entities) == ("리바로",)
    assert tuple(entity.canonical_id for entity in reconciled.entities) == (
        "리바로",
        "리바로젯",
        "로수젯",
        "리피토",
    )
    assert reconciled.comparison_targets == reconciled.entities
    assert reconciled.operation is QueryOperation.COMPARE_CURRENT
    assert reconciled.metrics == ("rank",)


def test_mixed_file_market_request_preserves_both_requested_facets() -> None:
    question = "내 파일에 있는 리바로 매출과 시스템 데이터를 비교해줘"

    spec = extract_query_spec(
        question,
        _AcceptanceResolver(),
        build_period_grounding(question),
    )

    assert spec.facets == (QueryFacet.FILE, QueryFacet.MARKET)
    assert query_spec_observation(spec)["facets"] == ["file", "market"]


def test_runtime_reconciliation_never_drops_preflight_entities() -> None:
    question = "아일리아와 비오뷰 매출 비교해줘"
    initial = extract_query_spec(
        question,
        _AcceptanceResolver(),
        build_period_grounding(question),
    )
    runtime = (
        BrandResolution(
            canonical_brand="아일리아",
            audit_code="runtime:아일리아",
            molecule_en=(),
            atc=(),
            edi_code=None,
            item_seq=None,
            is_combo=False,
        ),
    )

    reconciled = with_resolved_entities(initial, runtime)

    assert tuple(entity.canonical_id for entity in reconciled.entities) == (
        "아일리아",
        "비오뷰",
    )


@pytest.mark.parametrize(
    ("question", "expected"),
    (
        ("경쟁사 성장률", (CanonicalMetric.GROWTH,)),
        ("경쟁사 매출", (CanonicalMetric.SALES,)),
        ("경쟁사 점유율", (CanonicalMetric.SHARE,)),
        ("경쟁사 순위", (CanonicalMetric.RANK,)),
        ("경쟁사 순위 변화", (CanonicalMetric.RANK_CHANGE,)),
        ("처방량과 처방건수", (CanonicalMetric.PRESCRIPTION_VOLUME, CanonicalMetric.PRESCRIPTION_COUNT)),
        ("브랜드 단가", (CanonicalMetric.UNIT_PRICE,)),
        ("5년 CAGR", (CanonicalMetric.CAGR,)),
        ("시장 HHI", (CanonicalMetric.HHI,)),
    ),
)
def test_canonical_metric_vocabulary_is_stable(
    question: str,
    expected: tuple[CanonicalMetric, ...],
) -> None:
    spec = extract_query_spec(question, _AcceptanceResolver(), build_period_grounding(question))

    assert spec.metrics == expected


def test_rank_change_does_not_collapse_into_rank() -> None:
    question = "리바로 경쟁사 순위 변화 표로 보여줘"
    spec = extract_query_spec(question, _AcceptanceResolver(), build_period_grounding(question))

    assert spec.metrics == (CanonicalMetric.RANK_CHANGE,)
    assert CANONICAL_METRIC_DEFINITIONS[CanonicalMetric.GROWTH].calculation == "year_over_year"
    assert CANONICAL_METRIC_DEFINITIONS[CanonicalMetric.RANK_CHANGE].period_basis == "requested_period_range"


def test_cagr_wording_does_not_duplicate_yoy_growth_metric() -> None:
    question = "리바로 5년 연평균 성장률 알려줘"
    spec = extract_query_spec(question, _AcceptanceResolver(), build_period_grounding(question))

    assert spec.metrics == (CanonicalMetric.CAGR,)


@pytest.mark.parametrize(
    ("question", "operation", "metrics", "facets"),
    (
        (
            "이 시장 앞으로 어떻게 될 것 같아?",
            QueryOperation.CURRENT_VALUE,
            (),
            (QueryFacet.FORECAST,),
        ),
        (
            "리바로 시장 경쟁 구도가 최근 어떻게 변하고 있어?",
            QueryOperation.CURRENT_VALUE,
            (),
            (QueryFacet.COMPETITION_TREND,),
        ),
        (
            "리바로 어느 채널이나 진료과에서 잘 팔려?",
            QueryOperation.CURRENT_VALUE,
            ("sales",),
            (QueryFacet.CHANNEL,),
        ),
        (
            "리바로 시장 경쟁사 영업활동 변화 있어?",
            QueryOperation.CURRENT_VALUE,
            (),
            (QueryFacet.COMPETITOR_ACTIVITY, QueryFacet.COMPETITION_TREND),
        ),
        (
            "내 파일에 있는 리바로 매출과 시스템 데이터를 비교해줘",
            QueryOperation.COMPARE_CURRENT,
            ("sales",),
            (QueryFacet.FILE, QueryFacet.MARKET),
        ),
        (
            "리바로랑 리피토 중 누가 더 많이 팔렸어?",
            QueryOperation.COMPARE_CURRENT,
            ("sales",),
            (),
        ),
        (
            "리바로 vs 아토젯 매출과 순위를 비교해줘",
            QueryOperation.COMPARE_CURRENT,
            ("sales", "rank"),
            (),
        ),
        (
            "리바로, 리바로젯, 로수젯 매출을 나란히 보여줘",
            QueryOperation.COMPARE_CURRENT,
            ("sales",),
            (),
        ),
        (
            "리바로 매출 데이터의 시작일과 종료일 알려줘",
            QueryOperation.DATE_RANGE_BOUNDARY,
            ("sales",),
            (),
        ),
    ),
)
def test_g3_request_shape_is_preserved_before_planning(
    question: str,
    operation: QueryOperation,
    metrics: tuple[str, ...],
    facets: tuple[QueryFacet, ...],
) -> None:
    spec = extract_query_spec(
        question,
        BrandResolver(mode="fixture"),
        build_period_grounding(question),
    )

    assert spec.operation is operation
    assert spec.metrics == metrics
    assert spec.facets == facets


def test_ed04_preserves_all_requested_metrics_before_coverage_evaluation() -> None:
    question = (
        "리바로, 리바로젯, 로수젯, 리피토의 2022년부터 2024년까지 "
        "매출, 점유율, 순위, 성장률, 시장 규모를 비교해줘"
    )

    spec = extract_query_spec(
        question,
        BrandResolver(mode="fixture"),
        build_period_grounding(question),
    )

    assert spec.operation is QueryOperation.COMPARE_CURRENT
    assert spec.metrics == ("sales", "share", "market_size", "rank", "growth")
