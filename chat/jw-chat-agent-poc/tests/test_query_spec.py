from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from jw_chat_agent_poc.agent_loop.periods import build_period_grounding
from jw_chat_agent_poc.orchestrator.query_spec import (
    EntityKind,
    QueryOperation,
    TimeGranularity,
    extract_query_spec,
    query_spec_observation,
)
from jw_chat_agent_poc.resolver import BrandResolution


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
