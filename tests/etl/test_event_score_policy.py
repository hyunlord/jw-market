from __future__ import annotations

import logging

import pytest

from pipeline.etl.io.mart.event_score_policy import (
    REV5674_PROCESSOR,
    event_score_policy,
    is_cut_b_exposed,
    is_news_exposed,
)


@pytest.mark.parametrize(
    ("tag", "legacy_cutoff", "rev5674_cutoff"),
    [
        ("자본/경영", 43, 53),
        ("외부/트렌드", 49, 53),
        ("공급/생산", 51, 53),
        ("신약/R&D", 54, 73),
        ("정책/규제", 55, 69),
    ],
)
def test_news_exposure_uses_processor_specific_category_cutoffs(
    tag: str,
    legacy_cutoff: int,
    rev5674_cutoff: int,
) -> None:
    assert not is_news_exposed(tag=tag, score=legacy_cutoff - 1, source_processor=None)
    assert is_news_exposed(tag=tag, score=legacy_cutoff, source_processor=None)
    assert not is_news_exposed(
        tag=tag,
        score=rev5674_cutoff - 1,
        source_processor=REV5674_PROCESSOR,
    )
    assert is_news_exposed(
        tag=tag,
        score=rev5674_cutoff,
        source_processor=REV5674_PROCESSOR,
    )


@pytest.mark.parametrize("source_processor", [None, "workflow_196_optionB", REV5674_PROCESSOR])
def test_other_category_is_always_excluded(source_processor: str | None) -> None:
    assert not is_news_exposed(tag="기타", score=100, source_processor=source_processor)
    assert not is_news_exposed(tag="unrecognized", score=100, source_processor=source_processor)


def test_cut_b_uses_legacy_80_and_rev5674_88() -> None:
    assert not is_cut_b_exposed(score=79, source_processor=None)
    assert is_cut_b_exposed(score=80, source_processor=None)
    assert not is_cut_b_exposed(score=87, source_processor=REV5674_PROCESSOR)
    assert is_cut_b_exposed(score=88, source_processor=REV5674_PROCESSOR)


def test_unknown_processor_uses_legacy_policy_and_logs_once(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)

    first = event_score_policy("future_unknown_processor")
    second = event_score_policy("future_unknown_processor")

    assert first.cut_b_threshold == 80
    assert second.cut_b_threshold == 80
    warnings = [record for record in caplog.records if "future_unknown_processor" in record.getMessage()]
    assert len(warnings) == 1


def test_known_legacy_processor_does_not_emit_unknown_warning(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)

    policy = event_score_policy("tier2_llm_v1")

    assert policy.cut_b_threshold == 80
    assert caplog.records == []
