from __future__ import annotations

from agent2_density_worklist import (
    KNOWN_UNMATCHED_EVENT_BRANDS,
    build_brand_identities,
    build_evidence_counts_from_rows,
    route_density_worklist,
)


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


def test_evidence_counts_map_event_names_to_keys_and_exclude_unmatched_known_brands() -> None:
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
            "brand_canonical": "리조덱",
            "source_processor": "tier2_llm_v1",
            "derivation": "llm_direct",
            "tag": "신약/R&D",
            "score": 99,
        },
    ]

    result = build_evidence_counts_from_rows(brand_rows, score_rows)

    assert result.unmatched_known == ("리조덱",)
    assert result.unmatched_unknown == ()
    assert "리조덱" in KNOWN_UNMATCHED_EVENT_BRANDS
    assert [(row.brand, row.count, row.tag, row.score_cutoff) for row in result.counts] == [
        ("capital-key", 1, "자본/경영", 43)
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
