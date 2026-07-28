"""A number that names a row is not a claim about it.

Four rounds converged here. Routing, the tool contract and period intent were all
corrected, and a HIRA answer still came back as an 86-character refusal because
the ordinals numbering a template's rows — 1. 2. 3. 4. 5. — were harvested as
claims that no fact could attest. The same shape blocks age bands and code
ranges: "0-9세" contributed 0 and -9 while the 1,379명 beside it was fine.

The rule is structural, not a list of values: an ordinal is a marker followed by
whitespace and text at the start of a line, cell or bullet; a range label is two
bounds joined by a dash. Every measurement — patient counts, sales, share, HHI,
rank — stays a claim, and so do both numbers F66 deliberately blocks.
"""

from __future__ import annotations

import pytest

from jw_chat_agent_poc.service.evidence_binding_rules import (
    binding_claim_number_tokens,
    claim_number_tokens,
    excluded_label_token_count,
)

FIVE_STEP = """### 미보유 데이터 처리
| 단계 | 내용 |
| --- | --- |
| 1. 미보유 데이터 | 현재 채팅 조회 계약에 미노출된 지표입니다. |
| 2. 현재 가능한 proxy | 매출·점유율은 참고용 proxy로 조회할 수 있습니다. |
| 3. 해석 가능한 상한선 | 매출로 환자수를 역산하지 않습니다. |
| 4. 확인 필요 데이터 | 지표 종류, 기간, 브랜드가 필요합니다. |
| 5. 확보 시 수행할 분석 | 확보 시 기간·축별로 집계합니다. |
"""

AGE_BANDS = """| 연령구간 | 환자수 |
| --- | --- |
| 0-9세 | 1,379명 |
| 10-19세 | 2,258명 |
| 40-49세 | 5,120명 |
"""


# ------------------------------------------------- ordinals that number a row


def test_a_numbered_table_cell_stops_being_a_claim() -> None:
    """The live failure: gen1126 blocked exactly 1,2,3,4,5 on this template."""
    assert binding_claim_number_tokens(FIVE_STEP) == ()


@pytest.mark.parametrize(
    "text",
    [
        "1. 첫째\n2. 둘째\n3. 셋째",
        "- 3. 셋째\n* 4. 넷째",
        "  2) 둘째",
        "| 1. 미보유 데이터 | 내용 |",
    ],
)
def test_an_ordinal_marker_is_excluded_wherever_the_item_starts(text: str) -> None:
    assert binding_claim_number_tokens(text) == ()


def test_a_decimal_is_a_value_not_a_marker() -> None:
    """1.5 has no space after the point, so it is a measurement."""
    assert "1.5" in claim_number_tokens("| 값 | 1.5 |")


# ------------------------------------------------- range labels


def test_a_band_label_stops_being_a_claim_but_its_count_does_not() -> None:
    tokens = set(binding_claim_number_tokens(AGE_BANDS))

    assert {"1379명", "2258명", "5120명"} <= tokens
    assert not tokens & {"0", "-9", "10", "-19", "40", "-49"}


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        ("| 30-39 | 4,010명 |", "4010명"),
        ("| E10-E14 | 500명 |", "500명"),
        ("| 20~29세 | 3,012명 |", "3012명"),
    ],
)
def test_a_range_label_leaves_the_measurement_beside_it_alone(text: str, kept: str) -> None:
    tokens = set(binding_claim_number_tokens(text))

    assert kept in tokens
    assert tokens == {kept}


# ------------------------------------------------- ★ measurements must survive


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2024년 환자수는 1,305,727명입니다.", {"1305727명", "2024"}),
        ("리바로 2026-05 매출 80.39억원입니다.", {"80.39억원", "2026-05"}),
        ("점유율 9.13%입니다.", {"9.13%"}),
        ("HHI 3,188.0404 · CR5 29.52%입니다.", {"3188.0404", "29.52%"}),
        ("순위 6/555위입니다.", {"6", "555위"}),
        ("브랜드 0.95% · 시장 5.49% · 초과성장 -4.53%p", {"0.95%", "5.49%", "-4.53%p"}),
    ],
)
def test_a_measurement_is_still_a_claim(text: str, expected: set[str]) -> None:
    assert expected <= set(binding_claim_number_tokens(text))


def test_a_year_over_year_table_keeps_every_year_and_every_count() -> None:
    table = "| 연도 | 환자수 |\n| --- | --- |\n| 2020 | 3,334명 |\n| 2024 | 3,620명 |"

    assert {"2020", "2024", "3334명", "3620명"} <= set(binding_claim_number_tokens(table))


# ------------------------------------------------- ★ F66 must stay blocked


@pytest.mark.parametrize(
    ("text", "token"),
    [
        ("점유율 변화 0.17%p입니다.", "0.17%p"),
        ("매출 변화 0.76억원입니다.", "0.76억원"),
    ],
)
def test_the_two_numbers_f66_blocks_are_still_offered_for_binding(text: str, token: str) -> None:
    """F66 blocks these downstream. They must still reach it as claims."""
    assert token in binding_claim_number_tokens(text)


# ------------------------------------------------- periods are not range labels


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("기간 2026-01 ~ 2026-05", {"2026-01", "2026-05"}),
        ("2020-2024 기준", {"2020", "2024"}),
        ("3.93%(2025-12) 최고", {"3.93%", "2025-12"}),
    ],
)
def test_a_period_written_with_a_dash_is_not_mistaken_for_a_band(
    text: str, expected: set[str]
) -> None:
    """Periods are claimed before the range pass runs, so they survive it."""
    assert expected <= set(claim_number_tokens(text))


@pytest.mark.parametrize("text", ["상위 5개 브랜드", "최근 5년 추이", "2020년 매출"])
def test_a_quantity_in_prose_is_not_an_ordinal(text: str) -> None:
    assert claim_number_tokens(text)


# ------------------------------------------------- 요건③ the exclusion is visible


def test_the_number_of_excluded_tokens_is_reportable() -> None:
    assert excluded_label_token_count(FIVE_STEP) == 5
    assert excluded_label_token_count(AGE_BANDS) == 6


def test_an_answer_with_nothing_to_exclude_reports_zero() -> None:
    assert excluded_label_token_count("매출 80.39억원 점유율 9.13%") == 0


def test_excluding_everything_is_distinguishable_from_having_nothing() -> None:
    """요건④: an all-excluded answer must not read the same as a clean one."""
    all_excluded = excluded_label_token_count(FIVE_STEP), claim_number_tokens(FIVE_STEP)
    nothing_there = excluded_label_token_count("설명만 있습니다."), claim_number_tokens("설명만 있습니다.")

    assert all_excluded[1] == nothing_there[1] == ()
    assert all_excluded[0] == 5
    assert nothing_there[0] == 0
