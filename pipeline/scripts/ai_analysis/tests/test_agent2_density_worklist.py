from __future__ import annotations

from datetime import date

import pytest

from agent2_density_worklist import (
    MissingBrandMarketMembershipError,
    RoutedAgent2Brand,
    UnknownEventBrandError,
    build_brand_identities,
    build_central_evidence_from_rows,
    expand_market_scoped_worklist,
    route_density_worklist,
)
from bundle_builder.agent2_density_router import ProcessingMode, RouteDecision


def _brand_rows() -> list[dict]:
    return [
        {
            "brand_key": "ryzodeg-key",
            "brand_name": "리조덱플렉스터치",
            "raw_value_history": {"2026-04": 10},
        },
        {
            "brand_key": "zero-key",
            "brand_name": "제로브랜드",
            "raw_value_history": {"2026-04": 1},
        },
        {
            "brand_key": "winnerf-key",
            "brand_name": "위너프에이플러스",
            "raw_value_history": {"2026-04": 3},
        },
        {
            "brand_key": "tresiba-key",
            "brand_name": "트레시바플렉스터치",
            "raw_value_history": {"2026-04": 4},
        },
    ]


def _score(brand: str, news_id: str, score: int = 53) -> dict:
    return {
        "news_id": news_id,
        "brand_canonical": brand,
        "brand_name": brand,
        "mirrored_from_jw_brands": None,
        "source_processor": "workflow_196_rev5674",
        "derivation": "llm_direct",
        "tag": "자본/경영",
        "score": score,
        "published_date": date(2026, 7, 1),
        "joined_news_id": news_id,
    }


def test_brand_identity_uses_agent3_canonical_name_rule() -> None:
    rows = [
        {
            "brand_key": "bk-1",
            "brand_name": "낮은표기",
            "raw_value_history": {"2026-04": 1},
        },
        {
            "brand_key": "bk-1",
            "brand_name": "대표표기",
            "raw_value_history": {"2026-04": 10},
        },
    ]

    identities = build_brand_identities(rows)

    assert [(item.brand_key, item.canonical_brand_name) for item in identities] == [
        ("bk-1", "대표표기"),
    ]


def test_central_evidence_maps_aliases_and_records_registered_exclusions() -> None:
    result = build_central_evidence_from_rows(
        _brand_rows(),
        [
            _score("리조덱", "r"),
            _score("트레시바", "t"),
            _score("위너프A+", "w"),
            _score("염화칼륨", "e"),
            _score("오메가", "o"),
            _score("하트만", "h"),
        ],
    )

    assert [(row.brand_key, row.score.news_id) for row in result.score_rows] == [
        ("ryzodeg-key", "r"),
        ("tresiba-key", "t"),
        ("winnerf-key", "w"),
    ]
    assert result.excluded_registered == ("염화칼륨", "오메가", "하트만")
    assert result.unmatched_unknown == ()


def test_central_evidence_hard_fails_unknown_alias() -> None:
    with pytest.raises(UnknownEventBrandError, match="미등재"):
        build_central_evidence_from_rows(
            _brand_rows(),
            [_score("미등재", "unknown")],
        )


def test_central_evidence_routes_cross_mirror_with_shared_input_rule() -> None:
    row = _score("제로브랜드", "cross")
    row.update(
        {
            "derivation": "cross_match",
            "source_processor": "cross_match_adapter_v1",
            "mirrored_from_jw_brands": '["리조덱"]',
        }
    )

    result = build_central_evidence_from_rows(_brand_rows(), [row])

    assert [(item.brand_key, item.score.news_id) for item in result.score_rows] == [
        ("ryzodeg-key", "cross"),
        ("zero-key", "cross"),
    ]


def test_route_density_worklist_uses_central_cutoff_and_keeps_zero_brand() -> None:
    worklist = route_density_worklist(
        _brand_rows(),
        [_score("리조덱", "central-only", score=50)],
    )
    routed = {item.brand_key: item.route for item in worklist.routed}

    assert routed["ryzodeg-key"].evidence_count == 1
    assert routed["ryzodeg-key"].bucket == "sparse"
    assert routed["zero-key"].bucket == "zero"


def test_market_scoped_worklist_expands_every_catalog_membership() -> None:
    route = RouteDecision(
        "multi-key",
        2,
        "mid",
        ProcessingMode.LLM_COMPACT,
        ("tier2_llm_v1",),
    )
    routed = (
        RoutedAgent2Brand(
            brand_key="multi-key",
            canonical_brand_name="복수브랜드",
            route=route,
        ),
    )

    expanded = expand_market_scoped_worklist(
        routed,
        [
            {
                "general_brand_key": "multi-key",
                "ml_id": "ml_006",
                "strategy_id": "strategy_006",
            },
            {
                "general_brand_key": "multi-key",
                "ml_id": "ml_007",
                "strategy_id": "strategy_007",
            },
        ],
    )

    assert [
        (item.brand_key, item.requested_ml_id, item.requested_strategy_id, item.work_item_key)
        for item in expanded
    ] == [
        ("multi-key", "ml_006", "strategy_006", "multi-key::strategy_006"),
        ("multi-key", "ml_007", "strategy_007", "multi-key::strategy_007"),
    ]


def test_market_scoped_worklist_fails_when_catalog_membership_is_missing() -> None:
    route = RouteDecision(
        "unmatched-key",
        1,
        "sparse",
        ProcessingMode.LLM_FULL,
        ("tier2_llm_v1",),
    )
    routed = (
        RoutedAgent2Brand(
            brand_key="unmatched-key",
            canonical_brand_name="미매핑브랜드",
            route=route,
        ),
    )

    with pytest.raises(MissingBrandMarketMembershipError, match="unmatched-key"):
        expand_market_scoped_worklist(routed, [])
