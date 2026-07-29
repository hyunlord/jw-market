"""GPT6-TITLE-NORMALIZE: the strategic heading must appear exactly once.

The dual-view contract used to recognise only the exact canonical prefix, so an
answer that already opened with a strategic heading variant received a second
one. These tests pin the normalisation and, just as importantly, pin the cases
that must keep behaving exactly as before.
"""

from __future__ import annotations

import re

import pytest
from jw_chat_agent_poc.orchestrator.general_view_contract import DUAL_WARNING
from jw_chat_agent_poc.orchestrator.general_view_contract import STRATEGIC_HEADING
from jw_chat_agent_poc.orchestrator.general_view_contract import enforce_general_view_contract
from jw_chat_agent_poc.orchestrator.general_view_contract import strategic_heading_recognized

STRATEGIC_HEADING_RE = re.compile(r"(?m)^##\s*전략뷰.*$")
GENERAL_HEADING_RE = re.compile(r"(?m)^##\s*일반뷰.*$")

SECTION = "## 일반뷰 (ATC4)\n\n- 시장: ATC4 C10A0\n- 아일리아: 점유율 51.38%"


def _dual_contract() -> dict[str, object]:
    return {
        "mode": "dual",
        "section_markdown": SECTION,
        "atc4_code": "C10A0",
        "source": "IQVIA",
        "measure": "sales",
        "period": "2026-Q1",
    }


def _general_contract() -> dict[str, object]:
    return {**_dual_contract(), "mode": "general"}


def _strategic_headings(answer: str) -> list[str]:
    return STRATEGIC_HEADING_RE.findall(answer)


@pytest.mark.parametrize(
    "opening",
    (
        "## 전략뷰",
        "##  전략뷰 (market_landscape)",
        "## 전략뷰(market_landscape)",
        "## 전략뷰 ( market_landscape )",
        "  ## 전략뷰",
    ),
)
def test_recognised_heading_variant_is_not_duplicated(opening: str) -> None:
    answer = f"{opening}\n\n리바로 매출은 80.39억원입니다."

    result = enforce_general_view_contract(answer, _dual_contract())

    assert len(_strategic_headings(result)) == 1
    assert result.startswith(STRATEGIC_HEADING)
    assert "리바로 매출은 80.39억원입니다." in result


def test_answer_without_heading_still_receives_exactly_one() -> None:
    result = enforce_general_view_contract("리바로 매출은 80.39억원입니다.", _dual_contract())

    assert len(_strategic_headings(result)) == 1
    assert result.startswith(STRATEGIC_HEADING)


def test_canonical_heading_answer_is_unchanged_in_shape() -> None:
    answer = f"{STRATEGIC_HEADING}\n\n본문입니다."

    result = enforce_general_view_contract(answer, _dual_contract())

    assert len(_strategic_headings(result)) == 1
    assert result.startswith(f"{STRATEGIC_HEADING}\n\n본문입니다.")


@pytest.mark.parametrize(
    "opening",
    (
        "## 전략적 판단",
        "### 전략뷰",
        "## 일반뷰 (ATC4)",
    ),
)
def test_non_strategic_heading_is_preserved_and_prefixed(opening: str) -> None:
    answer = f"{opening}\n\n본문입니다."

    result = enforce_general_view_contract(answer, _dual_contract())

    assert len(_strategic_headings(result)) == 1
    assert result.startswith(STRATEGIC_HEADING)
    assert opening in result
    assert "본문입니다." in result


def test_titled_strategic_heading_keeps_its_own_words() -> None:
    """A heading carrying extra title words is left as content, not collapsed.

    Recognising "## 전략뷰 상세 분석" would mean replacing it with the canonical
    heading, which deletes "상세 분석". Callers also require the answer to start
    with the canonical heading (see test_service: startswith assertion), so the
    heading cannot simply be left in place either. Keeping it as body text is the
    only option that loses no content; the residual visual repetition is recorded
    as a known remaining variant rather than fixed by deleting words.
    """

    answer = "## 전략뷰 상세 분석\n\n본문입니다."

    result = enforce_general_view_contract(answer, _dual_contract())

    assert result.startswith(STRATEGIC_HEADING)
    assert "## 전략뷰 상세 분석" in result
    assert "본문입니다." in result


def test_body_mention_of_strategic_view_is_not_a_heading() -> None:
    answer = "이 답변은 전략뷰 기준입니다."

    result = enforce_general_view_contract(answer, _dual_contract())

    assert len(_strategic_headings(result)) == 1
    assert answer in result


def test_general_only_mode_gets_no_strategic_heading() -> None:
    result = enforce_general_view_contract("리바로 매출은 80.39억원입니다.", _general_contract())

    assert _strategic_headings(result) == []
    assert len(GENERAL_HEADING_RE.findall(result)) == 1


def test_missing_contract_leaves_answer_untouched() -> None:
    answer = "## 전략뷰\n\n본문입니다."

    assert enforce_general_view_contract(answer, None) == answer


def test_dual_contract_still_appends_section_label_and_warning() -> None:
    answer = "## 전략뷰\n\n본문입니다."

    result = enforce_general_view_contract(answer, _dual_contract())

    assert len(_strategic_headings(result)) == 1
    assert "## 일반뷰 (ATC4)" in result
    assert "기준: 일반뷰 (ATC4 C10A0) | 소스: IQVIA | 지표: sales | 기준: 2026-Q1" in result
    assert DUAL_WARNING in result


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        ("## 전략뷰", True),
        (STRATEGIC_HEADING, True),
        ("##  전략뷰 (market_landscape)", True),
        ("## 전략뷰(market_landscape)", True),
        ("  ## 전략뷰", True),
        ("## 전략적 판단", False),
        ("## 전략뷰 상세 분석", False),
        ("### 전략뷰", False),
        ("전략뷰", False),
        ("이 답변은 전략뷰 기준입니다.", False),
        ("## 일반뷰 (ATC4)", False),
        ("", False),
    ),
)
def test_strategic_heading_recognition_boundary(text: str, expected: bool) -> None:
    assert strategic_heading_recognized(text) is expected


def test_heading_only_answer_does_not_duplicate() -> None:
    result = enforce_general_view_contract("## 전략뷰", _dual_contract())

    assert len(_strategic_headings(result)) == 1
