"""A HIRA question that demands a span of years gets one.

"최근 5년" contains no trend word, and the executor asked for one year. The test
of a range request is whether it demands more than one period, not whether it
happens to contain 추이 — so the quantity, span, explicit-year and grain forms are
matched as shapes, and only two bare nouns are left enumerated.
"""

from __future__ import annotations

import pytest

from jw_chat_agent_poc.orchestrator.hira_disease import (
    HIRA_TREND_YEARS,
    hira_requested_years,
    hira_stat_requests,
)

HOSPITALIZATION = "hira_disease_hospitalization_outpatient_stats"
NAME_CODE = "hira_disease_name_code"


class _RecordingExternal:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def __getattr__(self, name: str):
        def record(*args: str):
            self.calls.append((name, *args))
            return name

        return record


def _call_years(question: str) -> list[str]:
    from jw_chat_agent_poc.orchestrator.hira_disease import _hira_external_calls

    external = _RecordingExternal()
    _hira_external_calls(question, external, "D693")
    return [call[2] for call in external.calls if len(call) > 2]


# ------------------------------------------------------- a span is served as a span


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("D693 상병 환자수 최근 5년 알려줘", HIRA_TREND_YEARS),
        ("D693 환자수 추이를 분석해줘", HIRA_TREND_YEARS),
        ("D693 환자수 시계열 알려줘", HIRA_TREND_YEARS),
        ("D693 환자수 최근 3년", ("2022", "2023", "2024")),
        ("D693 환자수 연도별로 알려줘", HIRA_TREND_YEARS),
        ("D693 환자수 2020~2024", HIRA_TREND_YEARS),
    ],
)
def test_a_question_that_demands_a_span_calls_every_year_in_it(
    question: str, expected: tuple[str, ...]
) -> None:
    assert hira_requested_years(question) == expected
    assert _call_years(question) == list(expected)


# ------------------------------------------------------- a single point stays single


@pytest.mark.parametrize(
    "question",
    [
        "D693 환자수 알려줘",
        "D693 작년 환자수",
        "D693 환자수 최근 12개월",
    ],
)
def test_a_question_that_names_no_span_gets_one_call(question: str) -> None:
    assert hira_requested_years(question) is None
    assert _call_years(question) == []


# ------------------------------------------------------- 요건④: 추이 is untouched


def test_a_trend_question_still_walks_exactly_the_same_years() -> None:
    external = _RecordingExternal()
    from jw_chat_agent_poc.orchestrator.hira_disease import _hira_external_calls

    _hira_external_calls("D693 환자수 추이를 분석해줘", external, "D693")

    assert external.calls == [
        (NAME_CODE, "D693"),
        (HOSPITALIZATION, "D693", "2020"),
        (HOSPITALIZATION, "D693", "2021"),
        (HOSPITALIZATION, "D693", "2022"),
        (HOSPITALIZATION, "D693", "2023"),
        (HOSPITALIZATION, "D693", "2024"),
    ]


# ------------------------------------------------------- the patterns reach further


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("D693 환자수 최근 4년", ("2021", "2022", "2023", "2024")),
        ("D693 환자수 3년간", ("2022", "2023", "2024")),
        ("D693 환자수 2021~2023", ("2021", "2022", "2023")),
        ("D693 환자수 해마다", HIRA_TREND_YEARS),
        ("D693 환자수 18개월간", ("2023", "2024")),
    ],
)
def test_a_shape_resolves_even_though_nobody_listed_this_wording(
    question: str, expected: tuple[str, ...]
) -> None:
    """No word here was enumerated; each matches a quantity, span, year or grain."""
    assert hira_requested_years(question) == expected


def test_a_span_longer_than_the_window_is_clipped_to_it() -> None:
    assert hira_requested_years("D693 환자수 최근 20년") == HIRA_TREND_YEARS


# ------------------------------------------------------- the stated coverage limit


@pytest.mark.parametrize("question", ["D693 환자수 트렌드", "D693 환자수 흐름", "D693 환자수 추세"])
def test_a_bare_synonym_outside_the_two_listed_words_is_not_reached(question: str) -> None:
    """The lexical axis holds 추이 and 시계열 and nothing else.

    Pinned so the limit is visible rather than discovered. Anything carrying a
    quantity, a unit or a grain never needs to be added here.
    """
    assert hira_requested_years(question) is None


# ------------------------------------------------------- 요건⑤ / F's result holds


@pytest.mark.parametrize(
    ("question", "expected_tools"),
    [
        ("D693 연령대별 환자수 알려줘", ["hira_disease_gender_age_stats"]),
        ("D693 기관종별 환자수 알려줘", ["hira_disease_institution_class_stats"]),
        ("D693 지역별 환자수 알려줘", ["hira_disease_area_stats"]),
        ("D693은 무슨 질환", []),
    ],
)
def test_the_tool_selection_f_established_is_unchanged(
    question: str, expected_tools: list[str]
) -> None:
    assert [request.tool for request in hira_stat_requests(question)] == expected_tools


def test_a_distribution_question_still_asks_for_all_four_at_one_point() -> None:
    requests = hira_stat_requests("D693 환자분포 알려줘")

    assert len(requests) == 4
    assert all(request.periods == () for request in requests)


# ------------------------------------------------------- 요건②③: the reason is stated


def test_a_resolved_span_names_the_span_it_resolved() -> None:
    request = hira_stat_requests("D693 환자수 최근 3년")[0]

    assert request.periods == ("2022", "2023", "2024")
    assert request.label == "HIRA 2022~2024 환자 추이"


def test_a_single_point_says_so_rather_than_falling_through_unlabelled() -> None:
    request = hira_stat_requests("D693 환자수 알려줘")[0]

    assert request.periods == ()
    assert request.label == "HIRA 입원/외래"


def test_the_same_question_resolves_to_the_same_span_every_time() -> None:
    question = "D693 상병 환자수 최근 5년 알려줘"

    assert len({hira_requested_years(question) for _ in range(5)}) == 1
