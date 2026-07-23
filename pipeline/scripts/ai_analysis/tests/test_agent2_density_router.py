from __future__ import annotations

import pytest

from pipeline.etl.io.mart.agent2_eligibility import (
    AGENT2_ELIGIBILITY_REVISION,
    Agent2ScoreRow,
)
from bundle_builder.agent2_density_router import (
    BrandedScoreRow,
    CATEGORY_SCORE_CUTOFFS,
    CATEGORY_SCORE_CUTOFFS_BY_VERSION,
    EvidenceCount,
    NEW_WF196_PROCESSOR,
    PENDING_TIER2_PROCESSOR,
    ProcessingMode,
    UnknownShadowBrandError,
    cutoff_for_tag,
    density_bucket,
    is_score_allowed_for_density,
    route_brand,
    route_worklist_with_shadow,
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


def test_route_worklist_with_shadow_records_central_difference_without_changing_routes() -> None:
    counts: tuple[EvidenceCount, ...] = ()
    score_rows = (
        BrandedScoreRow(
            brand_key="리바로",
            score=Agent2ScoreRow(
                news_id="news-central-only",
                source_processor=NEW_WF196_PROCESSOR,
                derivation="llm_direct",
                tag="자본/경영",
                score=50,
                published_date=None,
                news_exists=True,
            ),
        ),
    )

    result = route_worklist_with_shadow(("리바로",), counts, score_rows)

    assert result.routes == route_worklist(("리바로",), counts)
    assert result.shadow[0].brand_key == "리바로"
    assert result.shadow[0].density_news_ids == ()
    assert result.shadow[0].central_news_ids == ("news-central-only",)
    assert result.shadow[0].matches is False
    assert result.shadow[0].revision == AGENT2_ELIGIBILITY_REVISION


def test_route_worklist_with_shadow_uses_distinct_news_identity() -> None:
    score = Agent2ScoreRow(
        news_id="news-shared",
        source_processor="workflow_196_optionB",
        derivation="llm_direct",
        tag="자본/경영",
        score=43,
        published_date=None,
        news_exists=True,
    )

    result = route_worklist_with_shadow(
        ("리바로",),
        (),
        (
            BrandedScoreRow(brand_key="리바로", score=score),
            BrandedScoreRow(brand_key="리바로", score=score),
        ),
    )

    assert result.shadow[0].density_news_ids == ("news-shared",)
    assert result.shadow[0].central_news_ids == ("news-shared",)
    assert result.shadow[0].matches is True


def test_route_worklist_with_shadow_records_orphan_as_central_rejection_without_changing_routes() -> None:
    counts = (
        EvidenceCount(
            "리바로",
            "workflow_196_optionB",
            "llm_direct",
            1,
            tag="자본/경영",
            score_cutoff=43,
        ),
    )
    orphan = BrandedScoreRow(
        brand_key="리바로",
        score=Agent2ScoreRow(
            news_id="orphan-news",
            source_processor="workflow_196_optionB",
            derivation="llm_direct",
            tag="자본/경영",
            score=43,
            published_date=None,
            news_exists=False,
        ),
    )

    result = route_worklist_with_shadow(("리바로",), counts, (orphan,))

    assert result.routes == route_worklist(("리바로",), counts)
    assert result.shadow[0].density_news_ids == ("orphan-news",)
    assert result.shadow[0].central_news_ids == ()
    assert result.shadow[0].matches is False


def test_route_worklist_with_shadow_rejects_unknown_brand_key() -> None:
    score_rows = (
        BrandedScoreRow(
            brand_key="unmapped-brand",
            score=Agent2ScoreRow(
                news_id="news-1",
                source_processor="workflow_196_optionB",
                derivation="llm_direct",
                tag="자본/경영",
                score=43,
                published_date=None,
                news_exists=True,
            ),
        ),
    )

    with pytest.raises(UnknownShadowBrandError, match="unmapped-brand"):
        route_worklist_with_shadow(("리바로",), (), score_rows)


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


def test_tier2_v2_marker_uses_serving_cutoffs() -> None:
    assert PENDING_TIER2_PROCESSOR == "tier2_llm_v2_rev5671"
    assert CATEGORY_SCORE_CUTOFFS_BY_VERSION[PENDING_TIER2_PROCESSOR] == {
        "자본/경영": 41,
        "외부/트렌드": 48,
        "공급/생산": 22,
        "신약/R&D": 62,
        "정책/규제": 58,
    }
    assert cutoff_for_tag("기타", PENDING_TIER2_PROCESSOR) is None
    assert cutoff_for_tag("외부/트렌드", "unknown_future_marker") is None


@pytest.mark.parametrize(
    ("tag", "cutoff"),
    (
        ("자본/경영", 41),
        ("외부/트렌드", 48),
        ("공급/생산", 22),
        ("신약/R&D", 62),
        ("정책/규제", 58),
    ),
)
def test_tier2_v2_marker_enforces_each_cutoff_boundary(tag: str, cutoff: int) -> None:
    assert not is_score_allowed_for_density(cutoff - 1, tag, PENDING_TIER2_PROCESSOR)
    assert is_score_allowed_for_density(cutoff, tag, PENDING_TIER2_PROCESSOR)


def test_unknown_marker_remains_fail_closed() -> None:
    assert cutoff_for_tag("정책/규제", "unknown_future_marker") is None
    assert not is_score_allowed_for_density(100, "정책/규제", "unknown_future_marker")


def test_route_brand_applies_cutoff_for_each_processor_version() -> None:
    counts = (
        EvidenceCount("capital-key", "workflow_196_optionB", "llm_direct", 2, tag="자본/경영", score_cutoff=43),
        EvidenceCount("capital-key", NEW_WF196_PROCESSOR, "llm_direct", 3, tag="자본/경영", score_cutoff=53),
        EvidenceCount("capital-key", NEW_WF196_PROCESSOR, "llm_direct", 99, tag="자본/경영", score_cutoff=43),
    )

    route = route_brand("capital-key", counts)

    assert route.evidence_count == 5
    assert route.included_processors == ("workflow_196_optionB", NEW_WF196_PROCESSOR)
