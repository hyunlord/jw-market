from __future__ import annotations

from datetime import date

import pytest

from pipeline.etl.io.mart.agent2_eligibility import Agent2ScoreRow, OrphanNewsError
from bundle_builder import agent2_density_router
from bundle_builder.agent2_density_router import (
    BrandedScoreRow,
    ProcessingMode,
    UnknownScoreBrandError,
    density_bucket,
    route_worklist,
)


def _row(
    brand_key: str,
    news_id: str,
    *,
    score: int = 53,
    news_exists: bool = True,
) -> BrandedScoreRow:
    return BrandedScoreRow(
        brand_key=brand_key,
        score=Agent2ScoreRow(
            news_id=news_id,
            source_processor="workflow_196_rev5674",
            derivation="llm_direct",
            tag="자본/경영",
            score=score,
            published_date=date(2026, 7, 1),
            news_exists=news_exists,
        ),
    )


def test_density_bucket_maps_counts_to_processing_modes() -> None:
    assert density_bucket(10).mode is ProcessingMode.LLM_FULL
    assert density_bucket(3).mode is ProcessingMode.LLM_COMPACT
    assert density_bucket(1).mode is ProcessingMode.LLM_RECAP
    assert density_bucket(0).mode is ProcessingMode.TEMPLATE_ZERO


def test_route_worklist_uses_distinct_central_eligible_news_ids() -> None:
    routes = route_worklist(
        ("brand-1", "brand-zero"),
        (
            _row("brand-1", "shared", score=50),
            _row("brand-1", "shared", score=50),
            _row("brand-1", "below", score=42),
        ),
    )

    assert [route.brand for route in routes] == ["brand-1", "brand-zero"]
    assert routes[0].evidence_count == 1
    assert routes[0].mode is ProcessingMode.LLM_RECAP
    assert routes[0].included_processors == ("workflow_196_rev5674",)
    assert routes[1].mode is ProcessingMode.TEMPLATE_ZERO


def test_route_worklist_rejects_orphan_news() -> None:
    with pytest.raises(OrphanNewsError, match="orphan"):
        route_worklist(("brand-1",), (_row("brand-1", "orphan", news_exists=False),))


def test_route_worklist_rejects_unknown_brand_key() -> None:
    with pytest.raises(UnknownScoreBrandError, match="brand-2"):
        route_worklist(("brand-1",), (_row("brand-2", "news-1"),))


def test_route_worklist_evaluates_each_score_once(monkeypatch) -> None:
    calls = 0
    real_predicate = agent2_density_router.is_agent2_eligible

    def counted_predicate(row):
        nonlocal calls
        calls += 1
        return real_predicate(row)

    monkeypatch.setattr(
        agent2_density_router,
        "is_agent2_eligible",
        counted_predicate,
    )

    routes = route_worklist(
        ("brand-1", "brand-2", "brand-3"),
        (
            _row("brand-1", "news-1"),
            _row("brand-2", "news-2"),
        ),
    )

    assert calls == 2
    assert [route.evidence_count for route in routes] == [1, 1, 0]
