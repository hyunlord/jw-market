"""The live age band is written with an underscore, and the rule never saw it.

AXIS 2 was built for "0-9세" and "0~9세". HIRA sends "0_9세" … "40_49세" — five
labels, ten boundaries — sometimes prefixed by sex ("남 0_9세"). Every one of those
ten numbers reached binding as a claim, none of them had a fact behind it, and the
answer came back partial with blocked=10 while the 환자수 beside each label was
attestable the whole time.

The fix is one more alternative on the same shape, with the unit required instead
of optional. That requirement is the whole guard: a band carries 세/대/개월/년 after
its second bound, and the things that look like a band but are not — the period
"2020_2024", a digit-separated value like "200_000", a bare pair like "1_5" — do
not. The 1-3 digit bound stays for the same reason: "2020~2024" is a live period.
"""

from __future__ import annotations

import pytest

from jw_chat_agent_poc.service.evidence_binding_rules import (
    binding_claim_number_tokens,
    claim_number_tokens,
    excluded_label_token_count,
)

#: The five labels the deploy16 trace blocked, in the form HIRA sends them.
LIVE_BANDS: tuple[str, ...] = ("0_9세", "10_19세", "20_29세", "30_39세", "40_49세")

#: The ten boundaries those labels contributed, verbatim from the trace.
LIVE_BLOCKED: frozenset[str] = frozenset(
    {"0", "9", "10", "19", "20", "29", "30", "39", "40", "49"}
)

LIVE_AGE_TABLE = """| 구분 | 질병코드 | 기준연도 | 환자수 |
| --- | --- | --- | --- |
| 0_9세 | D693 | 2024 | 557 |
| 10_19세 | D693 | 2024 | 321 |
| 20_29세 | D693 | 2024 | 302 |
| 30_39세 | D693 | 2024 | 206 |
| 40_49세 | D693 | 2024 | 267 |
"""


# --------------------------------------------- 계열 1: the underscore band goes


@pytest.mark.parametrize("label", LIVE_BANDS)
def test_an_underscore_band_stops_being_a_claim(label: str) -> None:
    assert claim_number_tokens(label) == ()


def test_a_band_prefixed_by_sex_still_loses_only_its_boundaries() -> None:
    """The live chart label is '남 0_9세'; the band cannot be anchored to cell start."""
    assert claim_number_tokens("남 0_9세") == ()
    assert set(binding_claim_number_tokens("| 남 0_9세 | 557 |")) == {"557"}


def test_the_live_age_table_blocks_nothing_and_keeps_every_count() -> None:
    """The exact live failure: ten boundaries out, five 환자수 in."""
    tokens = set(binding_claim_number_tokens(LIVE_AGE_TABLE))

    assert {"557", "321", "302", "206", "267"} <= tokens
    assert not tokens & LIVE_BLOCKED


def test_a_band_nobody_has_seen_yet_matches_the_same_shape() -> None:
    """Not a value list: bands beyond the live five are covered too."""
    assert claim_number_tokens("| 60_69세 | 120명 |") == ("120명",)
    assert claim_number_tokens("| 70_79세 | 90명 |") == ("90명",)


@pytest.mark.parametrize("unit", ["세", "대", "개월", "년"])
def test_every_band_unit_is_covered(unit: str) -> None:
    assert claim_number_tokens(f"10_19{unit}") == ()


# --------------------------------------------- 계열 2: existing forms unchanged


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        ("| 0-9세 | 1,379명 |", "1379명"),
        ("| 0~9세 | 1,379명 |", "1379명"),
        ("| 30-39 | 4,010명 |", "4010명"),
        ("| E10-E14 | 500명 |", "500명"),
        ("| 20~29세 | 3,012명 |", "3012명"),
    ],
)
def test_the_dash_and_tilde_forms_behave_exactly_as_before(text: str, kept: str) -> None:
    assert set(binding_claim_number_tokens(text)) == {kept}


def test_a_dosing_interval_written_with_a_tilde_is_untouched_by_this_change() -> None:
    """'1~3개월' is live product text and was already excluded by the tilde branch."""
    assert claim_number_tokens("1~3개월") == ()


# --------------------------------------------- 계열 3: ★ true values must pass


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("| 0_9세 | 1,379명 |", {"1379명"}),
        ("2020년에는 외래 3,334명, 입원 561명이었습니다.", {"3334명", "561명", "2020"}),
        ("2024년 외래 3,620명, 입원 677명입니다.", {"3620명", "677명", "2024"}),
        ("리바로 2026-05 매출 80.39억원입니다.", {"80.39억원", "2026-05"}),
        ("점유율 9.13%입니다.", {"9.13%"}),
        ("HHI 3,188.0404 · CR5 29.52%입니다.", {"3188.0404", "29.52%"}),
        ("시장 규모 2,139.25억원입니다.", {"2139.25억원"}),
        ("순위 6/555위입니다.", {"6", "555위"}),
        ("고지혈증 시장 브랜드 555개입니다.", {"555개"}),
        ("초과성장 -4.53%p입니다.", {"-4.53%p"}),
        ("아일리아 매출 218.7억원입니다.", {"218.7억원"}),
        ("리바로 2025년 2분기 매출 242.72억원입니다.", {"242.72억원"}),
    ],
)
def test_a_measurement_is_still_a_claim(text: str, expected: set[str]) -> None:
    assert expected <= set(binding_claim_number_tokens(text))


def test_the_count_beside_an_underscore_band_survives_in_every_position() -> None:
    for count in ("557", "1,379명", "12,345명"):
        tokens = set(binding_claim_number_tokens(f"| 40_49세 | {count} |"))
        assert tokens == {count.replace(",", "")}


# --------------------------------------------- 계열 4: ★ F66 stays blocked


@pytest.mark.parametrize(
    ("text", "token"),
    [
        ("점유율 변화 0.17%p입니다.", "0.17%p"),
        ("매출 변화 0.76억원입니다.", "0.76억원"),
    ],
)
def test_the_two_numbers_f66_blocks_still_reach_binding(text: str, token: str) -> None:
    """F66 blocks these downstream. Releasing them here would undo that."""
    assert token in binding_claim_number_tokens(text)


# --------------------------------------------- 계열 5: ★ the traps


def test_an_underscore_year_range_is_a_period_not_a_band() -> None:
    """The 4-digit bound and the required unit both keep this a claim."""
    assert {"2020", "2024"} <= set(claim_number_tokens("2020_2024 기준"))


def test_a_live_tilde_year_range_is_still_a_period() -> None:
    """'2020~2024' appears in the 출처 table of the blocked-0 control answer."""
    assert {"2020", "2024"} <= set(claim_number_tokens("| 기준기간 | 2020~2024 |"))


@pytest.mark.parametrize("text", ["| 값 | 1_5 |", "| 값 | 200_000 |", "| 값 | 12_269 |"])
def test_a_digit_separated_value_is_not_a_band(text: str) -> None:
    """No unit after the second bound, so these stay measurements."""
    assert claim_number_tokens(text)


def test_an_underscore_code_range_is_not_covered_and_that_is_recorded() -> None:
    """§5⑤ 택일: E10_E14 does not occur anywhere, so the shape is left alone.

    Its numbers therefore remain claims. Documented as a coverage limit rather
    than fixed on an unobserved shape.
    """
    assert set(claim_number_tokens("| E10_E14 | 500명 |")) >= {"500명"}


def test_a_band_without_a_unit_is_not_covered_and_that_is_recorded() -> None:
    """'0_9' is indistinguishable from a value; requiring the unit is the guard."""
    assert claim_number_tokens("| 0_9 | 557 |")


# --------------------------------------------- 계열 6: AXIS 1 unchanged


FIVE_STEP = """### 미보유 데이터 처리
| 단계 | 내용 |
| --- | --- |
| 1. 미보유 데이터 | 현재 채팅 조회 계약에 미노출된 지표입니다. |
| 2. 현재 가능한 proxy | 매출·점유율은 참고용 proxy로 조회할 수 있습니다. |
| 3. 해석 가능한 상한선 | 매출로 환자수를 역산하지 않습니다. |
| 4. 확인 필요 데이터 | 지표 종류, 기간, 브랜드가 필요합니다. |
| 5. 확보 시 수행할 분석 | 확보 시 기간·축별로 집계합니다. |
"""


def test_the_ordinal_axis_is_untouched() -> None:
    assert binding_claim_number_tokens(FIVE_STEP) == ()
    assert excluded_label_token_count(FIVE_STEP) == 5


def test_a_decimal_is_still_a_value_not_a_marker() -> None:
    assert "1.5" in claim_number_tokens("| 값 | 1.5 |")


# --------------------------------------------- 계열 7: the exclusion is visible


def test_the_number_of_excluded_band_boundaries_is_reportable() -> None:
    """요건④: ten boundaries excluded, and the count says so."""
    assert excluded_label_token_count(LIVE_AGE_TABLE) == 10


def test_an_answer_with_no_band_reports_zero() -> None:
    assert excluded_label_token_count("매출 80.39억원 점유율 9.13%") == 0
