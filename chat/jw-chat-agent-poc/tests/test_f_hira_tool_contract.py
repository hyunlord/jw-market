"""The HIRA executor asks for what the verification contract requires.

Two rule sets used to decide which HIRA statistics tools a question needs: a
one-line `"추이" in question` test in the executor, and a seven-branch vocabulary
in the contract that never reached the executor. They disagreed on the default —
the contract asked for one tool, the executor called four — and the three extra
were banded tables whose axis labels destroyed the answer that contained them.

These tests pin the tool set, not the answer. Whether a banded answer can survive
the binder is a separate defect and is deliberately still failing.
"""

from __future__ import annotations

from collections import Counter

from jw_chat_agent_poc.orchestrator.hira_disease import (
    HIRA_TREND_YEARS,
    hira_stat_requests,
)
from jw_chat_agent_poc.orchestrator.tool_use_contract import tool_use_requirements


HOSPITALIZATION = "hira_disease_hospitalization_outpatient_stats"
GENDER_AGE = "hira_disease_gender_age_stats"
INSTITUTION = "hira_disease_institution_class_stats"
AREA = "hira_disease_area_stats"
NAME_CODE = "hira_disease_name_code"
BANDED = frozenset({GENDER_AGE, INSTITUTION, AREA})

TREND = "D693 환자수 추이를 분석해줘"
RECENT_FIVE = "D693 상병 환자수 최근 5년 알려줘"
TIME_SERIES = "D693 환자수 시계열 알려줘"
BY_AGE = "D693 연령대별 환자수 알려줘"


class _RecordingExternal:
    """Stands in for the external client and records the tool set requested."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def __getattr__(self, name: str):
        def record(*args: object):
            self.calls.append((name, args))
            return name

        return record

    def tool_names(self) -> list[str]:
        return [name for name, _ in self.calls]


def _executed(question: str) -> list[str]:
    from jw_chat_agent_poc.orchestrator.hira_disease import _hira_external_calls

    external = _RecordingExternal()
    _hira_external_calls(question, external, "D693")
    return external.tool_names()


def _requested(question: str) -> list[str]:
    return [request.tool for request in hira_stat_requests(question)]


# --------------------------------------------------- the executor obeys the contract


def test_a_plain_patient_count_asks_for_one_statistic_not_four() -> None:
    """The case that was destroying itself: no banded table is requested."""
    executed = _executed(RECENT_FIVE)

    assert executed == [NAME_CODE, HOSPITALIZATION]
    assert not BANDED.intersection(executed)


def test_c_a_synonym_of_trend_behaves_the_same_as_the_plain_question() -> None:
    """시계열 never matched the literal 추이; it must not fall to the banded sweep."""
    assert _executed(TIME_SERIES) == _executed(RECENT_FIVE)
    assert not BANDED.intersection(_executed(TIME_SERIES))


def test_d_another_disease_code_is_treated_identically() -> None:
    assert _executed("H360 환자수 알려줘") == [NAME_CODE, HOSPITALIZATION]


# --------------------------------------------------- 요건④: 추이 must not change


def test_b_a_trend_question_still_walks_every_year() -> None:
    executed = _executed(TREND)

    assert executed[0] == NAME_CODE
    assert Counter(executed)[HOSPITALIZATION] == len(HIRA_TREND_YEARS)
    assert len(executed) == 1 + len(HIRA_TREND_YEARS)
    assert not BANDED.intersection(executed)


def test_a_trend_question_requests_every_year_by_period() -> None:
    requests = hira_stat_requests(TREND)

    assert [request.tool for request in requests] == [HOSPITALIZATION]
    assert requests[0].periods == HIRA_TREND_YEARS


# --------------------------------------------------- the contract's own vocabulary


def test_an_explicit_breakdown_gets_that_breakdown_and_nothing_else() -> None:
    """e: the banded tool still attaches — the contract asks for it.

    This pins the tool set only. The answer is still expected to fail downstream,
    because a band label has no fact to bind to; that is a separate defect.
    """
    assert _requested(BY_AGE) == [GENDER_AGE]
    assert _executed(BY_AGE) == [NAME_CODE, GENDER_AGE]


def test_an_institution_question_gets_the_institution_statistic_alone() -> None:
    assert _executed("D693 기관종별 환자수 알려줘") == [NAME_CODE, INSTITUTION]


def test_a_region_question_gets_the_region_statistic_alone() -> None:
    assert _executed("D693 지역별 환자수 알려줘") == [NAME_CODE, AREA]


def test_a_distribution_question_is_the_only_one_that_asks_for_all_four() -> None:
    executed = _executed("D693 환자분포 알려줘")

    assert executed == [NAME_CODE, HOSPITALIZATION, GENDER_AGE, INSTITUTION, AREA]


def test_a_disease_identity_question_asks_for_no_statistic_at_all() -> None:
    assert _requested("D693은 무슨 질환") == []
    assert _executed("D693은 무슨 질환") == [NAME_CODE]


# --------------------------------------------------- 요건①③: one rule, one home


def test_the_contract_and_the_executor_cannot_disagree() -> None:
    """Both layers read the same function, so no question can split them."""
    questions = (
        TREND,
        RECENT_FIVE,
        TIME_SERIES,
        BY_AGE,
        "D693 기관종별 환자수 알려줘",
        "D693 지역별 환자수 알려줘",
        "D693 환자분포 알려줘",
        "H360 환자수 알려줘",
    )
    for question in questions:
        requested = set(_requested(question))
        executed = set(_executed(question)) - {NAME_CODE}
        assert requested == executed, question


def test_every_hira_requirement_the_contract_publishes_comes_from_the_rule() -> None:
    for question in (TREND, RECENT_FIVE, BY_AGE, "D693 환자분포 알려줘"):
        contract_tools = {
            tool
            for requirement in tool_use_requirements(question)
            for tool in requirement.alternatives
            if tool.startswith("hira_disease_") and tool != NAME_CODE
        }
        assert contract_tools == set(_requested(question)), question


def test_the_trend_literal_is_not_copied_a_fifth_time() -> None:
    """요건③: the executor must not carry its own copy of the keyword."""
    from pathlib import Path

    import jw_chat_agent_poc.orchestrator.hira_disease as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    assert source.count('"추이"') == 1


# --------------------------------------------------- 요건②: no silent catch-all


def test_a_question_the_vocabulary_does_not_recognise_still_names_its_reason() -> None:
    """f: an unrecognised question falls to a stated default, not to "call everything"."""
    requests = hira_stat_requests("D693 환자수 관련해서 뭐든 알려줘")

    assert [request.tool for request in requests] == [HOSPITALIZATION]
    assert requests[0].label
    assert not BANDED.intersection(request.tool for request in requests)
