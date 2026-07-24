from __future__ import annotations

from datetime import date

import pytest

from pipeline.etl.io.mart.agent2_eligibility import Agent2ScoreRow, OrphanNewsError
from pipeline.scripts.ai_analysis.agent2_brand_registry import (
    Agent2BrandRegistry,
    UnknownAgent2BrandAliasError,
    event_brand_match_sql,
    event_brand_source_names,
)
from pipeline.scripts.ai_analysis.agent2_density_worklist import (
    UnknownEventBrandError,
    build_central_evidence_from_rows,
)
from pipeline.scripts.ai_analysis.bundle_builder.agent2_density_router import (
    BrandedScoreRow,
    ProcessingMode,
    route_worklist,
)
from pipeline.scripts.ai_analysis.bundle_builder.event_bundle_builder import (
    select_central_bundle_rows,
)


def _score_row(
    news_id: str,
    *,
    score: int,
    source_processor: str = "workflow_196_rev5674",
    derivation: str = "llm_direct",
    tag: str = "자본/경영",
    news_exists: bool = True,
) -> Agent2ScoreRow:
    return Agent2ScoreRow(
        news_id=news_id,
        source_processor=source_processor,
        derivation=derivation,
        tag=tag,
        score=score,
        published_date=date(2026, 7, 1),
        news_exists=news_exists,
    )


def test_registry_maps_and_excludes_pl_approved_aliases() -> None:
    registry = Agent2BrandRegistry.for_canonical_names(
        {
            "리조덱",
            "리조덱플렉스터치",
            "트레시바",
            "트레시바플렉스터치",
            "리바로",
        }
    )

    assert registry.resolve("리조덱") == "리조덱플렉스터치"
    assert registry.resolve("트레시바") == "트레시바플렉스터치"
    assert registry.resolve("염화칼륨") is None
    assert registry.resolve("하트만") is None
    assert registry.resolve("오메가") is None
    assert registry.source_names_for("리조덱플렉스터치") == (
        "리조덱",
        "리조덱플렉스터치",
    )


def test_registry_hard_fails_unregistered_alias() -> None:
    registry = Agent2BrandRegistry.for_canonical_names({"리바로"})

    with pytest.raises(UnknownAgent2BrandAliasError, match="미등재"):
        registry.resolve("미등재")


def test_central_brand_input_selection_includes_cross_mirrors_only_for_cross_rows() -> None:
    direct = {
        "brand_canonical": "직접정본",
        "brand_name": "직접표기",
        "derivation": "llm_direct",
        "mirrored_from_jw_brands": '["무시할미러"]',
    }
    cross = {
        **direct,
        "derivation": "cross_match",
        "mirrored_from_jw_brands": '["미러B", "미러A"]',
    }

    assert event_brand_source_names(direct) == ("직접정본", "직접표기")
    assert event_brand_source_names(cross) == (
        "미러A",
        "미러B",
        "직접정본",
        "직접표기",
    )


def test_central_brand_sql_uses_same_three_source_fields() -> None:
    sql, params = event_brand_match_sql(("리바로",))

    assert "brand_canonical IN" in sql
    assert "brand_name IN" in sql
    assert "derivation = 'cross_match'" in sql
    assert "mirrored_from_jw_brands LIKE" in sql
    assert params == ("리바로", "리바로", '%"리바로"%')


def test_density_routes_distinct_central_news_ids() -> None:
    rows = (
        BrandedScoreRow("brand-1", _score_row("central-only", score=50)),
        BrandedScoreRow("brand-1", _score_row("central-only", score=50)),
        BrandedScoreRow("brand-1", _score_row("below", score=42)),
    )

    route = route_worklist(("brand-1",), rows)[0]

    assert route.evidence_count == 1
    assert route.mode is ProcessingMode.LLM_RECAP
    assert route.included_processors == ("workflow_196_rev5674",)


def test_density_propagates_central_orphan_hard_fail() -> None:
    rows = (BrandedScoreRow("brand-1", _score_row("orphan", score=99, news_exists=False)),)

    with pytest.raises(OrphanNewsError, match="orphan"):
        route_worklist(("brand-1",), rows)


def test_bundle_selection_uses_central_eligibility_and_effective_selector() -> None:
    rows = (
        {
            "news_id": "central-only",
            "published_date": date(2026, 7, 1),
            "score": 48,
            "tag": "외부/트렌드",
            "source_processor": "workflow_196_rev5674",
            "derivation": "llm_direct",
            "joined_news_id": "central-only",
        },
        {
            "news_id": "below",
            "published_date": date(2026, 7, 2),
            "score": 47,
            "tag": "외부/트렌드",
            "source_processor": "workflow_196_rev5674",
            "derivation": "llm_direct",
            "joined_news_id": "below",
        },
    )

    selection = select_central_bundle_rows(
        rows,
        snapshot_date=date(2026, 7, 24),
        lookback_months=6,
        direct_cap=30,
        cross_cap=5,
        deduplicate_direct_by_date=True,
    )

    assert tuple(row["news_id"] for row in selection.direct_rows) == ("central-only",)
    assert selection.cross_rows == ()


def test_bundle_selection_projects_the_same_best_row_used_for_distinct_news_id() -> None:
    rows = (
        {
            "news_id": "duplicate",
            "published_date": date(2026, 7, 1),
            "score": 48,
            "tag": "외부/트렌드",
            "source_processor": "workflow_196_rev5674",
            "derivation": "llm_direct",
            "joined_news_id": "duplicate",
            "title": "lower",
        },
        {
            "news_id": "duplicate",
            "published_date": date(2026, 7, 2),
            "score": 60,
            "tag": "외부/트렌드",
            "source_processor": "workflow_196_rev5674",
            "derivation": "llm_direct",
            "joined_news_id": "duplicate",
            "title": "higher",
        },
    )

    selection = select_central_bundle_rows(
        rows,
        snapshot_date=date(2026, 7, 24),
        lookback_months=6,
        direct_cap=30,
        cross_cap=5,
        deduplicate_direct_by_date=True,
    )

    assert selection.direct_rows[0]["title"] == "higher"


def test_bundle_selection_never_projects_ineligible_duplicate_row() -> None:
    rows = (
        {
            "news_id": "duplicate",
            "published_date": date(2026, 7, 1),
            "score": 53,
            "tag": "자본/경영",
            "source_processor": "workflow_196_rev5674",
            "derivation": "llm_direct",
            "joined_news_id": "duplicate",
            "title": "eligible",
        },
        {
            "news_id": "duplicate",
            "published_date": date(2026, 7, 2),
            "score": 99,
            "tag": "기타",
            "source_processor": "workflow_196_rev5674",
            "derivation": "llm_direct",
            "joined_news_id": "duplicate",
            "title": "ineligible",
        },
    )

    selection = select_central_bundle_rows(
        rows,
        snapshot_date=date(2026, 7, 24),
        lookback_months=6,
        direct_cap=30,
        cross_cap=5,
        deduplicate_direct_by_date=True,
    )

    assert selection.direct_rows[0]["title"] == "eligible"


def test_worklist_maps_exclusions_and_hard_fails_unknowns() -> None:
    brand_rows = [
        {
            "brand_key": "ryzodeg-key",
            "brand_name": "리조덱플렉스터치",
            "raw_value_history": {"2026-06": 1},
        }
    ]
    base = {
        "news_id": "news-1",
        "source_processor": "workflow_196_rev5674",
        "derivation": "llm_direct",
        "tag": "자본/경영",
        "score": 53,
        "published_date": date(2026, 7, 1),
        "joined_news_id": "news-1",
    }
    result = build_central_evidence_from_rows(
        brand_rows,
        [
            {**base, "brand_canonical": "리조덱"},
            {**base, "news_id": "news-2", "joined_news_id": "news-2", "brand_canonical": "염화칼륨"},
        ],
    )

    assert tuple(row.brand_key for row in result.score_rows) == ("ryzodeg-key",)
    assert result.excluded_registered == ("염화칼륨",)
    assert result.unmatched_unknown == ()

    with pytest.raises(UnknownEventBrandError, match="미등재"):
        build_central_evidence_from_rows(
            brand_rows,
            [{**base, "brand_canonical": "미등재"}],
        )
