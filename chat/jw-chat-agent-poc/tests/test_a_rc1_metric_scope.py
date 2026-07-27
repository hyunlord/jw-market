from __future__ import annotations

import pytest

from jw_chat_agent_poc.service.evidence_binding_rules import (
    claim_metrics_for_token,
    metric_matches,
    token_unit,
)


# Prose shapes reproduced in A-1. The metric word and the amount share one
# sentence, which is what makes the segment branch hand the amount the wrong
# metric. Table shapes are deliberately absent here: A-0 established the live
# tier is segment, and F82 failed by reproducing with a table.
MIXED_SHARE_SENTENCE = "리바로 점유율은 3.76% 수준이며 90.86억원입니다."
MIXED_MS_ABBREV = "리바로 MS는 3.76%이고 90.86억원입니다."
MIXED_RANK_AND_SHARE = "리바로는 6위이고 점유율 3.76%, 90.86억원입니다."
PERIOD_IN_SHARE_SENTENCE = "2025-12 리바로 점유율은 3.93%로 낮아졌습니다."


class _Fact:
    """Minimal stand-in for an EvidenceFact for metric_matches."""

    def __init__(self, metric: str) -> None:
        self.metric = metric


# ----- the RC1 five: a currency amount must not be expected as a share -----


@pytest.mark.parametrize(
    "answer",
    [MIXED_SHARE_SENTENCE, MIXED_MS_ABBREV, MIXED_RANK_AND_SHARE],
)
def test_currency_token_is_expected_as_sales_not_share(answer: str) -> None:
    metrics = claim_metrics_for_token(answer, "90.86억원")
    assert "시장점유율" not in metrics, "a 억원 amount is not a share"
    assert metrics == ("매출",)


@pytest.mark.parametrize("token", ["90.86억원", "75.08억원"])
def test_currency_token_passes_against_a_sales_fact(token: str) -> None:
    metrics = claim_metrics_for_token(MIXED_SHARE_SENTENCE.replace("90.86억원", token), token)
    assert metric_matches(_Fact("매출"), metrics) is True


def test_period_token_is_expected_as_period() -> None:
    metrics = claim_metrics_for_token(PERIOD_IN_SHARE_SENTENCE, "2025-12")
    assert metrics == ("기간",)


@pytest.mark.parametrize("token", ["2025-12", "2026-02", "2025-08"])
def test_period_token_passes_the_metric_axis(token: str) -> None:
    answer = PERIOD_IN_SHARE_SENTENCE.replace("2025-12", token)
    metrics = claim_metrics_for_token(answer, token)
    # rules.py:252-253 exempts an expectation of exactly ("기간",)
    assert metric_matches(_Fact("매출"), metrics) is True


# ----- F66 must keep blocking. These are the STOP conditions. -----


def test_f66_share_delta_stays_blocked() -> None:
    """0.17%p is a derived 점유율 변화; expecting the base metric keeps it blocked."""
    answer = "리바로 점유율은 3.76%이고 초과성장 0.17%p입니다."
    metrics = claim_metrics_for_token(answer, "0.17%p")
    assert "점유율 변화" not in metrics, "must not expect the derived metric"
    assert metric_matches(_Fact("점유율 변화"), metrics) is False


def test_f66_sales_delta_stays_blocked() -> None:
    """0.76억원 is a derived 매출 변화; a 매출 expectation still refuses it."""
    answer = "리바로 매출은 80.39억원이고 증가분은 0.76억원입니다."
    metrics = claim_metrics_for_token(answer, "0.76억원")
    assert "매출 변화" not in metrics, "must not expect the derived metric"
    assert metric_matches(_Fact("매출 변화"), metrics) is False


# ----- single-metric sentences must not move -----


def test_single_sales_sentence_unchanged() -> None:
    assert claim_metrics_for_token("리바로 매출은 90.86억원입니다.", "90.86억원") == ("매출",)


def test_single_share_sentence_unchanged() -> None:
    assert claim_metrics_for_token("리바로 점유율은 3.76%입니다.", "3.76%") == ("시장점유율",)


def test_market_size_sentence_unchanged() -> None:
    """시장규모 is also a 억원 metric and must survive the narrowing."""
    assert claim_metrics_for_token(
        "리바로가 속한 시장규모는 2,139.25억원입니다.", "2139.25억원"
    ) == ("시장규모",)


def test_unitless_token_is_untouched() -> None:
    """No unit in the table -> today's behaviour, unchanged."""
    answer = "리바로 점유율은 3.76%이고 HHI는 253.62입니다."
    assert claim_metrics_for_token(answer, "253.62") == claim_metrics_for_token(answer, "253.62")
    assert "시장점유율" in claim_metrics_for_token(answer, "253.62")


def test_segment_without_any_metric_still_yields_nothing() -> None:
    assert claim_metrics_for_token("값은 90.86억원입니다.", "90.86억원") == ()


# ----- the F24 table path must behave exactly as before -----


TWO_TABLES = (
    "| 기간 | 매출 |\n| --- | --- |\n| 2025-08 | 90.86억원 |\n\n"
    "| 구분 | MS |\n| --- | --- |\n| x | 90.86억원 |\n"
)


def test_f24_union_across_tables_is_preserved() -> None:
    """The union is the F24 RC1 fix; this round must not narrow it."""
    assert claim_metrics_for_token(TWO_TABLES, "90.86억원") == ("매출", "시장점유율")


def test_single_table_header_path_is_preserved() -> None:
    table = "| 기간 | 매출 | MS |\n| --- | --- | --- |\n| 2025-08 | 90.86억원 | 3.93% |\n"
    assert claim_metrics_for_token(table, "90.86억원") == ("매출",)
    assert claim_metrics_for_token(table, "2025-08") == ("기간",)


def test_token_unit_is_unchanged_by_this_round() -> None:
    assert token_unit("90.86억원") == "억원"
    assert token_unit("0.17%p") == "%p"
    assert token_unit("2025-12") == ""
    assert token_unit("3.76%") == "%"
