from __future__ import annotations

from bundle_builder.agent2_density_router import (
    EvidenceCount,
    ProcessingMode,
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
