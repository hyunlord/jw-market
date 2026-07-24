from __future__ import annotations

from datetime import date

from pipeline.etl.io.mart.agent2_eligibility import Agent2ScoreRow
from pipeline.scripts.ai_analysis.bundle_builder.agent2_bundle_shadow import (
    LEGACY_BUNDLE_SCORE_CUTOFF,
)
from pipeline.scripts.ai_analysis.bundle_builder.event_bundle_builder import (
    compare_event_bundle_selection_with_shadow,
)


def _row(
    news_id: str,
    *,
    score: int,
    tag: str = "자본/경영",
    derivation: str = "llm_direct",
    source_processor: str = "workflow_196_rev5674",
    published_date: date = date(2026, 7, 1),
    news_exists: bool = True,
) -> Agent2ScoreRow:
    return Agent2ScoreRow(
        news_id=news_id,
        source_processor=source_processor,
        derivation=derivation,
        tag=tag,
        score=score,
        published_date=published_date,
        news_exists=news_exists,
    )


def test_bundle_shadow_records_central_difference_without_changing_legacy_selection() -> None:
    result = compare_event_bundle_selection_with_shadow(
        "brand-1",
        (
            _row(
                "central-only",
                score=48,
                tag="외부/트렌드",
                published_date=date(2026, 7, 1),
            ),
            _row(
                "shared",
                score=50,
                tag="외부/트렌드",
                published_date=date(2026, 7, 2),
            ),
        ),
        snapshot_date=date(2026, 7, 22),
    )

    assert LEGACY_BUNDLE_SCORE_CUTOFF == 50
    assert result.bundle_news_ids == ("shared",)
    assert result.central_news_ids == ("central-only", "shared")
    assert result.matches is False


def test_bundle_shadow_applies_legacy_lookback_dedup_and_caps() -> None:
    rows = tuple(
        _row(
            f"direct-{index:02d}",
            score=100 - index,
            published_date=date(2026, 7, 1 + (index % 2)),
        )
        for index in range(35)
    ) + tuple(
        _row(
            f"cross-{index:02d}",
            score=100 - index,
            derivation="cross_match",
            source_processor="cross_match_adapter_v1",
            published_date=date(2026, 6, 1),
        )
        for index in range(8)
    ) + (
        _row("outside", score=100, published_date=date(2025, 12, 31)),
    )

    result = compare_event_bundle_selection_with_shadow(
        "brand-1",
        rows,
        snapshot_date=date(2026, 7, 22),
    )

    assert len(result.bundle_direct_news_ids) == 2
    assert len(result.bundle_cross_news_ids) == 5
    assert "outside" not in result.bundle_news_ids
    assert len(result.central_direct_news_ids) == 2
    assert len(result.central_cross_news_ids) == 5


def test_bundle_shadow_uses_distinct_news_identity_and_is_deterministic() -> None:
    rows = (
        _row("same", score=51),
        _row("same", score=50),
        _row("other", score=52, published_date=date(2026, 7, 2)),
    )

    first = compare_event_bundle_selection_with_shadow(
        "brand-1",
        rows,
        snapshot_date=date(2026, 7, 22),
    )
    second = compare_event_bundle_selection_with_shadow(
        "brand-1",
        tuple(reversed(rows)),
        snapshot_date=date(2026, 7, 22),
    )

    assert first == second
    assert first.bundle_news_ids == ("other", "same")
    assert first.central_news_ids == ("other", "same")
    assert first.matches is True


def test_bundle_shadow_records_orphan_rejection_without_failing_legacy_path() -> None:
    result = compare_event_bundle_selection_with_shadow(
        "brand-1",
        (
            _row("orphan", score=99, news_exists=False),
            _row("joined", score=50),
        ),
        snapshot_date=date(2026, 7, 22),
    )

    assert result.bundle_news_ids == ("joined",)
    assert result.central_news_ids == ("joined",)
    assert result.central_orphan_news_ids == ("orphan",)
    assert result.matches is True


def test_bundle_shadow_keeps_direct_and_cross_identity_separate_before_union() -> None:
    result = compare_event_bundle_selection_with_shadow(
        "brand-1",
        (
            _row("direct", score=50),
            _row(
                "cross",
                score=50,
                derivation="cross_match",
                source_processor="cross_match_adapter_v1",
            ),
        ),
        snapshot_date=date(2026, 7, 22),
    )

    assert result.bundle_direct_news_ids == ("direct",)
    assert result.bundle_cross_news_ids == ("cross",)
    assert result.central_direct_news_ids == ("direct",)
    assert result.central_cross_news_ids == ("cross",)
