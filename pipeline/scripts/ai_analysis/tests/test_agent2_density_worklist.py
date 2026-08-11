from __future__ import annotations

import pytest

from agent2_density_worklist import (
    JW_BRANDS,
    KNOWN_UNMATCHED_EVENT_BRANDS,
    UnknownEventBrandError,
    build_brand_identities,
    build_evidence_counts_from_rows,
    route_density_worklist,
)


def test_weekly_worklist_orders_jw_then_strategic_then_other_deterministically() -> None:
    jw_rows = [
        {
            "brand_key": f"jw-{index:02d}",
            "brand_name": "위너프에이플러스" if brand == "위너프A+" else brand,
            "raw_value_history": {"2026-06": 1000 - index},
        }
        for index, brand in enumerate(sorted(JW_BRANDS))
    ]
    brand_rows = jw_rows + [
        {"brand_key": "strategy-low", "brand_name": "전략저매출", "raw_value_history": {"2026-06": 10}},
        {"brand_key": "strategy-high", "brand_name": "전략고매출", "raw_value_history": {"2026-06": 20}},
        {"brand_key": "other", "brand_name": "기타", "raw_value_history": {"2026-06": 9999}},
    ]
    strategic_rows = [
        {"canonical_name": "전략저매출", "is_jw": 0, "is_target": 1},
        {"canonical_name": "전략고매출", "is_jw": 0, "is_target": 1},
    ]

    first = route_density_worklist(
        brand_rows,
        [],
        weekly_global=True,
        strategic_rows=strategic_rows,
    )
    second = route_density_worklist(
        list(reversed(brand_rows)),
        [],
        weekly_global=True,
        strategic_rows=list(reversed(strategic_rows)),
    )

    first_keys = [item.brand_key for item in first.routed]
    assert first_keys == [item.brand_key for item in second.routed]
    assert len(first.routed[:25]) == 25
    assert all(item.tier == 0 for item in first.routed[:25])
    assert first_keys[25:27] == ["strategy-high", "strategy-low"]
    assert first_keys[-1] == "other"
    assert [item.cohort for item in first.routed[:27]] == ["jw"] * 25 + ["strategic"] * 2


def test_weekly_opt_in_records_alias_and_all_non_jw_exclusions() -> None:
    brand_rows = [
        {
            "brand_key": "종근당자누비아",
            "brand_name": "종근당 자누비아",
            "raw_value_history": {"2026-Q1": 10},
        }
    ]
    score_rows = [
        {
            "brand_canonical": "종근당자누비아",
            "source_processor": "tier2_llm_v1",
            "derivation": "llm_direct",
            "tag": "신약/R&D",
            "score": 99,
        },
        *[
            {
                "brand_canonical": "노보믹스",
                "source_processor": "tier2_llm_v1",
                "derivation": "llm_direct",
                "tag": "신약/R&D",
                "score": 99,
            }
            for _ in range(3)
        ],
    ]

    worklist = route_density_worklist(
        brand_rows,
        score_rows,
        weekly_global=True,
        strategic_rows=[
            {
                "canonical_name": "종근당 자누비아",
                "is_jw": 0,
                "is_target": 1,
                "ml_market_id": "ml_003",
                "cd_market_id": "cd_003",
            }
        ],
    )

    assert worklist.evidence.unmatched_unknown == ()
    assert worklist.evidence.aliases == (
        ("종근당자누비아", "종근당자누비아", "ml_003", "cd_003"),
    )
    assert [item.to_dict() for item in worklist.evidence.excluded] == [
        {
            "brand": "노보믹스",
            "reason": "excluded_non_jw_market",
            "source_event_count": 3,
        }
    ]


def test_brand_identity_uses_agent3_canonical_name_rule() -> None:
    rows = [
        {"brand_key": "bk-1", "brand_name": "낮은표기", "raw_value_history": {"2026-04": 1}},
        {"brand_key": "bk-1", "brand_name": "대표표기", "raw_value_history": {"2026-04": 10}},
        {"brand_key": "bk-2", "brand_name": "제로브랜드", "raw_value_history": {"2026-04": 0}},
    ]

    identities = build_brand_identities(rows)

    assert [(item.brand_key, item.canonical_brand_name) for item in identities] == [
        ("bk-1", "대표표기"),
        ("bk-2", "제로브랜드"),
    ]


def test_evidence_counts_map_event_names_to_keys_and_exclude_omega_as_unmatched_known() -> None:
    brand_rows = [
        {"brand_key": "capital-key", "brand_name": "자본브랜드", "raw_value_history": {"2026-04": 10}},
        {"brand_key": "etc-key", "brand_name": "기타브랜드", "raw_value_history": {"2026-04": 5}},
    ]
    score_rows = [
        {
            "brand_canonical": "자본브랜드",
            "source_processor": "tier2_llm_v1",
            "derivation": "llm_direct",
            "tag": "자본/경영",
            "score": 43,
        },
        {
            "brand_canonical": "기타브랜드",
            "source_processor": "tier2_llm_v1",
            "derivation": "llm_direct",
            "tag": "기타",
            "score": 99,
        },
        {
            "brand_canonical": "오메가",
            "source_processor": "tier2_llm_v1",
            "derivation": "llm_direct",
            "tag": "신약/R&D",
            "score": 99,
        },
    ]

    result = build_evidence_counts_from_rows(brand_rows, score_rows)

    assert result.unmatched_known == ("오메가",)
    assert result.unmatched_unknown == ()
    assert "오메가" in KNOWN_UNMATCHED_EVENT_BRANDS
    assert [(row.brand, row.count, row.tag, row.score_cutoff) for row in result.counts] == [
        ("capital-key", 1, "자본/경영", 43)
    ]


def test_evidence_counts_resolve_pl_confirmed_rizodec_alias() -> None:
    brand_rows = [
        {
            "brand_key": "리조덱플렉스터치",
            "brand_name": "리조덱플렉스터치",
            "raw_value_history": {"2026-04": 10},
        },
    ]
    score_rows = [
        {
            "brand_canonical": "리조덱",
            "source_processor": "tier2_llm_v1",
            "derivation": "llm_direct",
            "tag": "신약/R&D",
            "score": 99,
        }
        for _ in range(40)
    ]

    result = build_evidence_counts_from_rows(brand_rows, score_rows)

    assert "리조덱" not in KNOWN_UNMATCHED_EVENT_BRANDS
    assert result.unmatched_known == ()
    assert result.unmatched_unknown == ()
    assert [(row.brand, row.count, row.tag, row.score_cutoff) for row in result.counts] == [
        ("리조덱플렉스터치", 40, "신약/R&D", 54)
    ]


def test_evidence_counts_resolve_pl_confirmed_winnerf_a_plus_alias() -> None:
    brand_rows = [
        {"brand_key": "winnerf-a-plus-key", "brand_name": "위너프에이플러스", "raw_value_history": {"2026-04": 10}},
    ]
    score_rows = [
        {
            "brand_canonical": "위너프A+",
            "source_processor": "workflow_196_optionB",
            "derivation": "llm_direct",
            "tag": "신약/R&D",
            "score": 54,
        },
        {
            "brand_canonical": "트레시바",
            "source_processor": "tier2_llm_v1",
            "derivation": "llm_direct",
            "tag": "신약/R&D",
            "score": 99,
        },
    ]

    result = build_evidence_counts_from_rows(brand_rows, score_rows)

    assert "위너프A+" not in KNOWN_UNMATCHED_EVENT_BRANDS
    assert result.unmatched_known == ("트레시바",)
    assert result.unmatched_unknown == ()
    assert [(row.brand, row.count, row.tag, row.score_cutoff) for row in result.counts] == [
        ("winnerf-a-plus-key", 1, "신약/R&D", 54)
    ]


def test_route_density_worklist_returns_brand_key_routes_with_display_names() -> None:
    brand_rows = [
        {"brand_key": "capital-key", "brand_name": "자본브랜드", "raw_value_history": {"2026-04": 10}},
        {"brand_key": "zero-key", "brand_name": "제로브랜드", "raw_value_history": {"2026-04": 1}},
    ]
    score_rows = [
        {
            "brand_canonical": "자본브랜드",
            "source_processor": "tier2_llm_v1",
            "derivation": "llm_direct",
            "tag": "자본/경영",
            "score": 43,
        }
    ]

    worklist = route_density_worklist(brand_rows, score_rows)

    assert [(item.brand_key, item.canonical_brand_name, item.route.bucket) for item in worklist.routed] == [
        ("capital-key", "자본브랜드", "sparse"),
        ("zero-key", "제로브랜드", "zero"),
    ]
    assert worklist.evidence.unmatched_known == ()


def test_route_density_worklist_skips_seven_unknown_brands_below_one_percent() -> None:
    brand_rows = [
        {
            "brand_key": f"brand-{index:04d}",
            "brand_name": f"brand-{index:04d}",
            "raw_value_history": {"2026-Q1": 1},
        }
        for index in range(1000)
    ]
    score_rows = [
        {
            "brand_canonical": f"unknown-{index}",
            "source_processor": "tier2_llm_v1",
            "derivation": "llm_direct",
            "tag": "신약/R&D",
            "score": 99,
        }
        for index in range(7)
    ]

    worklist = route_density_worklist(brand_rows, score_rows)

    assert len(worklist.routed) == 1000
    assert worklist.evidence.unmatched_unknown == tuple(
        f"unknown-{index}" for index in range(7)
    )


def test_route_density_worklist_allows_exactly_one_percent_unknown_brands() -> None:
    brand_rows = [
        {
            "brand_key": f"brand-{index:04d}",
            "brand_name": f"brand-{index:04d}",
            "raw_value_history": {"2026-Q1": 1},
        }
        for index in range(100)
    ]
    score_rows = [
        {
            "brand_canonical": "unknown-boundary",
            "source_processor": "tier2_llm_v1",
            "derivation": "llm_direct",
            "tag": "신약/R&D",
            "score": 99,
        }
    ]

    worklist = route_density_worklist(brand_rows, score_rows)

    assert len(worklist.routed) == 100
    assert worklist.evidence.unmatched_unknown == ("unknown-boundary",)


def test_route_density_worklist_fails_when_unknown_brands_exceed_one_percent() -> None:
    brand_rows = [
        {
            "brand_key": f"brand-{index:04d}",
            "brand_name": f"brand-{index:04d}",
            "raw_value_history": {"2026-Q1": 1},
        }
        for index in range(100)
    ]
    score_rows = [
        {
            "brand_canonical": f"unknown-{index}",
            "source_processor": "tier2_llm_v1",
            "derivation": "llm_direct",
            "tag": "신약/R&D",
            "score": 99,
        }
        for index in range(2)
    ]

    with pytest.raises(UnknownEventBrandError, match=r"2/100.*2\.0000%.*1\.0000%"):
        route_density_worklist(brand_rows, score_rows)


def test_exact_brand_key_event_is_routed_with_explicit_opt_in() -> None:
    brand_rows = [
        {
            "brand_key": "레미닐피알서방",
            "brand_name": "레미닐 피알 서방",
            "raw_value_history": {"2026-Q1": 10},
        }
    ]
    score_rows = [
        {
            "brand_canonical": "레미닐피알서방",
            "source_processor": "tier2_llm_v1",
            "derivation": "llm_direct",
            "tag": "신약/R&D",
            "score": 99,
        }
    ]

    worklist = route_density_worklist(
        brand_rows,
        score_rows,
        accept_canonical_brand_keys=True,
    )

    assert worklist.evidence.unmatched_unknown == ()
    assert [(item.brand_key, item.canonical_brand_name) for item in worklist.routed] == [
        ("레미닐피알서방", "레미닐 피알 서방")
    ]


def test_evidence_counts_branch_cutoff_by_wf196_processor() -> None:
    brand_rows = [
        {"brand_key": "capital-key", "brand_name": "자본브랜드", "raw_value_history": {"2026-04": 10}},
    ]
    score_rows = [
        {
            "brand_canonical": "자본브랜드",
            "source_processor": "workflow_196_optionB",
            "derivation": "llm_direct",
            "tag": "자본/경영",
            "score": 50,
        },
        {
            "brand_canonical": "자본브랜드",
            "source_processor": "workflow_196_rev5674",
            "derivation": "llm_direct",
            "tag": "자본/경영",
            "score": 50,
        },
        {
            "brand_canonical": "자본브랜드",
            "source_processor": "workflow_196_rev5674",
            "derivation": "llm_direct",
            "tag": "자본/경영",
            "score": 53,
        },
    ]

    result = build_evidence_counts_from_rows(brand_rows, score_rows)

    assert [(row.source_processor, row.count, row.score_cutoff) for row in result.counts] == [
        ("workflow_196_optionB", 1, 43),
        ("workflow_196_rev5674", 1, 53),
    ]
