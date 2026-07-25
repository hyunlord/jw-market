from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pipeline.scripts.crawler.crawl_backlog_policy import (
    PendingItem,
    PendingSnapshot,
    assess_backlog,
)


NOW = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)


def _snapshot(
    *keys: tuple[str, str],
    age_days: int = 0,
) -> PendingSnapshot:
    first_seen = NOW - timedelta(days=age_days)
    return PendingSnapshot(
        captured_at=NOW,
        items=tuple(
            PendingItem(news_id=news_id, brand_canonical=brand, first_seen_at=first_seen)
            for news_id, brand in keys
        ),
    )


def test_hard_gate_accepts_a_decreasing_backlog_without_new_unresolved_pairs() -> None:
    before = _snapshot(("n1", "a"), ("n2", "b"))
    after = _snapshot(("n1", "a"))

    assessment = assess_backlog(before=before, after=after, prior_after_counts=())

    assert assessment.hard_pass is True
    assert assessment.pending_delta == -1
    assert assessment.new_unresolved_count == 0


def test_hard_gate_rejects_growth_and_same_count_replacement() -> None:
    before = _snapshot(("n1", "a"))
    growth = _snapshot(("n1", "a"), ("n2", "b"))
    replacement = _snapshot(("n2", "b"))

    grown = assess_backlog(before=before, after=growth, prior_after_counts=())
    replaced = assess_backlog(before=before, after=replacement, prior_after_counts=())

    assert grown.hard_pass is False
    assert grown.pending_delta == 1
    assert grown.new_unresolved_count == 1
    assert replaced.hard_pass is False
    assert replaced.pending_delta == 0
    assert replaced.new_unresolved_count == 1


def test_age_slo_warns_at_two_days_and_fails_at_four_days() -> None:
    before = _snapshot(("n1", "a"))

    warning = assess_backlog(
        before=before,
        after=_snapshot(("n1", "a"), age_days=2),
        prior_after_counts=(),
    )
    failure = assess_backlog(
        before=before,
        after=_snapshot(("n1", "a"), age_days=4),
        prior_after_counts=(),
    )

    assert warning.slo_status == "warning"
    assert warning.slo_warnings == ("oldest_pending_age_days>=2",)
    assert failure.slo_status == "failure"
    assert failure.slo_failures == ("oldest_pending_age_days>=4",)


def test_trend_slo_warns_at_two_runs_and_fails_at_four_runs() -> None:
    before = _snapshot(("n1", "a"))
    after = _snapshot(("n1", "a"))

    warning = assess_backlog(before=before, after=after, prior_after_counts=(1,))
    failure = assess_backlog(before=before, after=after, prior_after_counts=(1, 1, 1))

    assert warning.nondecreasing_runs == 2
    assert "nondecreasing_runs>=2" in warning.slo_warnings
    assert failure.nondecreasing_runs == 4
    assert "nondecreasing_runs>=4" in failure.slo_failures


def test_zero_backlog_resets_age_and_trend_slos() -> None:
    assessment = assess_backlog(
        before=_snapshot(("n1", "a")),
        after=_snapshot(),
        prior_after_counts=(3, 2, 1),
    )

    assert assessment.hard_pass is True
    assert assessment.slo_status == "ok"
    assert assessment.oldest_pending_age_days is None
    assert assessment.nondecreasing_runs == 0
