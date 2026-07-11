from __future__ import annotations

import logging

import pytest

from pipeline.etl.io.mart.event_score_policy import (
    LEGACY_CATEGORY_CUTOFFS,
    LEGACY_POLICY,
    REV5674_CATEGORY_CUTOFFS,
    REV5674_PROCESSOR,
    REV5674_POLICY,
    TIER2_CATEGORY_CUTOFFS,
    TIER2_LLM_V1_PROCESSOR,
    TIER2_LLM_V2_PROCESSOR,
    TIER2_POLICY,
    event_score_policy,
    is_cut_b_exposed,
    is_news_exposed,
    news_exposure_sql_predicate,
)


APPROVED_LEGACY_CUTOFFS = {
    "자본/경영": 43,
    "외부/트렌드": 49,
    "공급/생산": 51,
    "신약/R&D": 54,
    "정책/규제": 55,
}
APPROVED_REV5674_CUTOFFS = {
    "자본/경영": 43,
    "외부/트렌드": 48,
    "공급/생산": 43,
    "신약/R&D": 58,
    "정책/규제": 54,
}
APPROVED_TIER2_CUTOFFS = {
    "자본/경영": 41,
    "외부/트렌드": 48,
    "공급/생산": 22,
    "신약/R&D": 62,
    "정책/규제": 58,
}


def test_policy_tables_match_the_approved_exposure_spec() -> None:
    assert dict(LEGACY_CATEGORY_CUTOFFS) == APPROVED_LEGACY_CUTOFFS
    assert dict(REV5674_CATEGORY_CUTOFFS) == APPROVED_REV5674_CUTOFFS
    assert dict(TIER2_CATEGORY_CUTOFFS) == APPROVED_TIER2_CUTOFFS
    assert REV5674_POLICY.cut_b_threshold == 88
    assert TIER2_POLICY.cut_b_threshold == 88


@pytest.mark.parametrize(
    ("source_processor", "approved_cutoffs"),
    [
        (None, APPROVED_LEGACY_CUTOFFS),
        ("future_unknown_processor", APPROVED_LEGACY_CUTOFFS),
        (REV5674_PROCESSOR, APPROVED_REV5674_CUTOFFS),
        (TIER2_LLM_V1_PROCESSOR, APPROVED_TIER2_CUTOFFS),
        (TIER2_LLM_V2_PROCESSOR, APPROVED_TIER2_CUTOFFS),
    ],
)
def test_news_exposure_holds_every_approved_boundary(
    source_processor: str | None,
    approved_cutoffs: dict[str, int],
) -> None:
    for tag, cutoff in approved_cutoffs.items():
        assert not is_news_exposed(tag=tag, score=cutoff - 1, source_processor=source_processor)
        assert is_news_exposed(tag=tag, score=cutoff, source_processor=source_processor)
        assert is_news_exposed(tag=tag, score=cutoff + 1, source_processor=source_processor)


@pytest.mark.parametrize(
    "source_processor",
    [
        None,
        "workflow_196_optionB",
        "future_unknown_processor",
        REV5674_PROCESSOR,
        TIER2_LLM_V1_PROCESSOR,
        TIER2_LLM_V2_PROCESSOR,
    ],
)
def test_other_category_is_always_excluded(source_processor: str | None) -> None:
    assert not is_news_exposed(tag="기타", score=100, source_processor=source_processor)
    assert not is_news_exposed(tag="unrecognized", score=100, source_processor=source_processor)


def test_cut_b_uses_legacy_80_and_rev5674_88() -> None:
    assert not is_cut_b_exposed(score=79, source_processor=None)
    assert is_cut_b_exposed(score=80, source_processor=None)
    assert not is_cut_b_exposed(score=87, source_processor=REV5674_PROCESSOR)
    assert is_cut_b_exposed(score=88, source_processor=REV5674_PROCESSOR)


@pytest.mark.parametrize("source_processor", [TIER2_LLM_V1_PROCESSOR, TIER2_LLM_V2_PROCESSOR])
def test_tier2_processors_share_the_approved_policy(source_processor: str) -> None:
    assert event_score_policy(source_processor) is TIER2_POLICY
    assert not is_cut_b_exposed(score=87, source_processor=source_processor)
    assert is_cut_b_exposed(score=88, source_processor=source_processor)


def test_unknown_processor_uses_legacy_policy_and_logs_once(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    event_score_policy.cache_clear()

    first = event_score_policy("future_unknown_processor")
    second = event_score_policy("future_unknown_processor")

    assert first is LEGACY_POLICY
    assert second is LEGACY_POLICY
    warnings = [record for record in caplog.records if "future_unknown_processor" in record.getMessage()]
    assert len(warnings) == 1


def test_known_processors_do_not_emit_unknown_warning(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)

    tier2_v1 = event_score_policy(TIER2_LLM_V1_PROCESSOR)
    tier2_v2 = event_score_policy(TIER2_LLM_V2_PROCESSOR)
    legacy = event_score_policy("workflow_196_optionB")

    assert tier2_v1 is TIER2_POLICY
    assert tier2_v2 is TIER2_POLICY
    assert legacy is LEGACY_POLICY
    assert caplog.records == []


def test_news_exposure_sql_predicate_has_rev5674_tier2_and_legacy_branches() -> None:
    sql, params = news_exposure_sql_predicate("s")

    assert "s.tag <> %s" in sql
    assert "s.source_processor = %s" in sql
    assert "s.source_processor IN (%s, %s)" in sql
    assert "s.source_processor IS NULL OR" in sql
    assert "s.source_processor <> %s AND s.source_processor <> %s AND s.source_processor <> %s" in sql
    assert params[0] == "기타"
    assert params.count(REV5674_PROCESSOR) == 2
    assert params.count(TIER2_LLM_V1_PROCESSOR) == 2
    assert params.count(TIER2_LLM_V2_PROCESSOR) == 2
    for approved_cutoffs in (
        APPROVED_REV5674_CUTOFFS,
        APPROVED_TIER2_CUTOFFS,
        APPROVED_LEGACY_CUTOFFS,
    ):
        for tag, cutoff in approved_cutoffs.items():
            assert tag in params
            assert cutoff in params


def test_news_exposure_sql_predicate_rejects_unsafe_alias() -> None:
    with pytest.raises(ValueError):
        news_exposure_sql_predicate("s;DROP")
