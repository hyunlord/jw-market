from __future__ import annotations

from bundle_builder.agent2_density_router import (
    CATEGORY_SCORE_CUTOFFS,
    CATEGORY_SCORE_CUTOFFS_BY_VERSION,
    EvidenceCount,
    NEW_WF196_PROCESSOR,
    PENDING_TIER2_PROCESSOR,
    ProcessingMode,
    cutoff_for_tag,
    density_bucket,
    route_brand,
    route_worklist,
)


def test_density_bucket_maps_score50_counts_to_processing_modes() -> None:
    assert density_bucket(10).mode is ProcessingMode.LLM_FULL
    assert density_bucket(3).mode is ProcessingMode.LLM_COMPACT
    assert density_bucket(1).mode is ProcessingMode.LLM_RECAP
    assert density_bucket(0).mode is ProcessingMode.TEMPLATE_ZERO


def test_route_brand_uses_allowed_score50_evidence_only() -> None:
    counts = (
        EvidenceCount("리바로", "workflow_196_optionB", "llm_direct", 9),
        EvidenceCount("리바로", "cross_match_adapter_v1", "cross_match", 1),
        EvidenceCount("리바로", "tier2_exact_rule_v1", "llm_direct", 99),
        EvidenceCount("리바로", "workflow_196_optionB", "llm_direct", 4, score_cutoff=40),
    )

    route = route_brand("리바로", counts)

    assert route.evidence_count == 10
    assert route.bucket == "full"
    assert route.mode is ProcessingMode.LLM_FULL
    assert route.included_processors == ("workflow_196_optionB", "cross_match_adapter_v1")


def test_route_worklist_keeps_zero_brands_in_template_queue() -> None:
    routes = route_worklist(
        ("리바로", "제로브랜드"),
        (EvidenceCount("리바로", "workflow_196_optionB", "llm_direct", 2),),
    )

    assert [route.brand for route in routes] == ["리바로", "제로브랜드"]
    assert [route.bucket for route in routes] == ["sparse", "zero"]
    assert routes[1].mode is ProcessingMode.TEMPLATE_ZERO


def test_route_brand_uses_category_cutoffs_and_excludes_etc() -> None:
    counts = (
        EvidenceCount("capital-key", "tier2_llm_v1", "llm_direct", 2, tag="자본/경영", score_cutoff=43),
        EvidenceCount("capital-key", "workflow_196_optionB", "llm_direct", 99, tag="기타", score_cutoff=1),
        EvidenceCount("capital-key", "workflow_196_optionB", "llm_direct", 99, tag="자본/경영", score_cutoff=50),
    )

    route = route_brand("capital-key", counts)

    assert CATEGORY_SCORE_CUTOFFS["자본/경영"] == 43
    assert cutoff_for_tag("기타") is None
    assert route.evidence_count == 2
    assert route.bucket == "sparse"
    assert route.included_processors == ("tier2_llm_v1",)


def test_rev5674_processor_uses_pl_confirmed_cutoffs() -> None:
    assert CATEGORY_SCORE_CUTOFFS_BY_VERSION[NEW_WF196_PROCESSOR] == {
        "자본/경영": 53,
        "외부/트렌드": 53,
        "공급/생산": 53,
        "신약/R&D": 73,
        "정책/규제": 69,
    }
    assert cutoff_for_tag("자본/경영", NEW_WF196_PROCESSOR) == 53
    assert cutoff_for_tag("신약/R&D", NEW_WF196_PROCESSOR) == 73
    assert cutoff_for_tag("정책/규제", NEW_WF196_PROCESSOR) == 69
    assert cutoff_for_tag("기타", NEW_WF196_PROCESSOR) is None


def test_unmapped_tier2_v2_marker_is_explicitly_fail_closed() -> None:
    assert PENDING_TIER2_PROCESSOR == "tier2_llm_v2_rev5671"
    assert PENDING_TIER2_PROCESSOR not in CATEGORY_SCORE_CUTOFFS_BY_VERSION
    assert cutoff_for_tag("정책/규제", PENDING_TIER2_PROCESSOR) is None
    assert cutoff_for_tag("외부/트렌드", "unknown_future_marker") is None


def test_route_brand_applies_cutoff_for_each_processor_version() -> None:
    counts = (
        EvidenceCount("capital-key", "workflow_196_optionB", "llm_direct", 2, tag="자본/경영", score_cutoff=43),
        EvidenceCount("capital-key", NEW_WF196_PROCESSOR, "llm_direct", 3, tag="자본/경영", score_cutoff=53),
        EvidenceCount("capital-key", NEW_WF196_PROCESSOR, "llm_direct", 99, tag="자본/경영", score_cutoff=43),
    )

    route = route_brand("capital-key", counts)

    assert route.evidence_count == 5
    assert route.included_processors == ("workflow_196_optionB", NEW_WF196_PROCESSOR)
