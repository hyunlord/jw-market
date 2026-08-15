"""R14 STAGE 4 — a comparison question reaches MIXED, and a MIXED answer keeps
the numbers its file leg actually retrieved.

Scope guard: the FILE isolation path is deliberately untouched. These tests pin
that a file-only or market-only question routes exactly as before.
"""
from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

from jw_chat_agent_poc.service import numeric_copy_contract as N
from jw_chat_agent_poc.service.context_scope import (
    ContextScope,
    has_file_reference,
    resolve_context_scope,
)

CHSO_COLUMNS = (
    "AUDIT DESC", "MFR NAME KOR", "PRODUCT NAME KOR", "PACK DESCRIPTION",
    "CHC 1", "ATC 4", "VALUES LC SI PRICE\n1/2025",
)


def _scope(question: str, *, market: bool = True, active_file: bool = True):
    return resolve_context_scope(
        question,
        has_active_file=active_file,
        has_market_intent=market,
        has_market_anchor=market,
        file_schema_columns=CHSO_COLUMNS,
    )


# --- routing: a file named by title, not by demonstrative -----------------

@pytest.mark.parametrize(
    "question",
    [
        "CHSO 문서의 액티넘 매출과 우리 마트 데이터를 비교해줘",
        "CHSO 파일의 매출과 내부 데이터를 비교해줘",
        "3월 보고서의 수치와 자사 데이터 차이 알려줘",
    ],
)
def test_a_file_named_by_its_own_title_reaches_mixed(question):
    assert has_file_reference(question)
    assert _scope(question) is ContextScope.MIXED


@pytest.mark.parametrize(
    "question",
    [
        "업로드 파일의 액티넘 매출과 시장 데이터를 비교해줘",
        "이 문서의 액티넘 매출과 mart에서 본 값을 비교해줘",
    ],
)
def test_the_previously_supported_phrasings_still_reach_mixed(question):
    assert _scope(question) is ContextScope.MIXED


# --- routing: nothing else moves ------------------------------------------

@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("리바로 매출 알려줘", ContextScope.MARKET),
        ("2025년 박카스디 매출 알려줘", ContextScope.MARKET),
        ("전체 시장 규모 알려줘", ContextScope.MARKET),
        # Naming a file without asking for a comparison stays file-scoped, so
        # the isolation path this round must not disturb still governs it.
        ("CHSO 문서의 액티넘 매출 알려줘", ContextScope.FILE),
        ("업로드 파일의 액티넘 매출 알려줘", ContextScope.FILE),
    ],
)
def test_questions_that_are_not_comparisons_route_exactly_as_before(question, expected):
    market = expected is not ContextScope.FILE
    assert _scope(question, market=market) is expected


def test_a_session_without_an_uploaded_file_is_unaffected():
    assert _scope("리바로 매출 알려줘", active_file=False) is ContextScope.MARKET


def test_a_bare_noun_is_not_mistaken_for_a_named_file():
    # "문서" with no qualifier and no demonstrative is not a file reference.
    assert not has_file_reference("문서 작성 방법 알려줘")


def test_only_the_genitive_form_is_recognised():
    """Wider particles would re-route a corpus case frozen in the write-once
    pre-cutover asset routing_inputs.v3.json. Widening is a separate decision."""
    assert has_file_reference("CHSO 문서의 매출")
    assert not has_file_reference("내 파일에 있는 리바로 매출과 시스템 데이터를 비교해줘")


# --- the FILE isolation path is byte-identical ----------------------------

_PINNED = {
    "_enforce_file_scope_isolation":
        "ca83fbcf91e4cafeed5122c82b0bb1448f7165ae1a80e9e9bb8f5f2462cdc6ef",
    "_enforce_file_postprocess_isolation":
        "0eefad457ab35317a139d3ae4d5fc464d85b223afafa672dadbee09e55a3847e",
    "_FILE_MARKET_POSTPROCESS_RE":
        "70401fae2b7c316c96e7a458d96aa352c4ddaba1544b6195c07e60df38549540",
}


def test_the_file_isolation_symbols_are_unchanged():
    """These three guard an incident where market prose contaminated a file
    answer. Widening the MIXED entrance must not reach them."""
    source = (
        Path(__file__).resolve().parents[1]
        / "jw_chat_agent_poc" / "service" / "app.py"
    ).read_text()
    tree = ast.parse(source)
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in _PINNED:
            segment = ast.get_source_segment(source, node)
            found[node.name] = hashlib.sha256(segment.encode()).hexdigest()
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in _PINNED:
                    segment = ast.get_source_segment(source, node)
                    found[target.id] = hashlib.sha256(segment.encode()).hexdigest()
    assert found == _PINNED


# --- MIXED numeric grounding ----------------------------------------------

def _mixed_result(file_context: str, *, market_calls=None) -> dict:
    return {
        "context_scope": "MIXED",
        "mixed_market_result": {
            "answer": "리바로 매출은 83.18억원입니다.",
            "sources": ["UBIST"],
            "tool_calls": market_calls or [],
            "markdown_response": {},
        },
        "mixed_file_result": {
            "sources": ["document"],
            "tool_calls": [],
            "file_context": file_context,
        },
    }


def test_a_number_the_file_leg_retrieved_survives_the_numeric_contract():
    result = _mixed_result(
        "The annual sales outlook for 2026 is exactly KRW 120.0 billion (1,200억원)."
    )
    answer = (
        "## 시장 데이터\n\n없음\n\n"
        "## 첨부 문서 — report.pdf\n\n"
        "The annual sales outlook for 2026 is exactly KRW 120.0 billion (1,200억원)."
    )
    kept, report = N.enforce_numeric_copy_contract("q", answer, result)
    assert "1,200억원" in kept
    assert report["disposition"] == "pass"


def test_a_number_absent_from_both_legs_is_still_blocked():
    result = _mixed_result("The outlook is KRW 120.0 billion (1,200억원).")
    answer = "## 첨부 문서 — report.pdf\n\n매출은 9,999억원입니다."
    kept, report = N.enforce_numeric_copy_contract("q", answer, result)
    assert "9,999억원" not in kept
    assert report["disposition"] == "blocked"


def test_the_market_leg_still_needs_its_own_evidence():
    """Admitting file evidence must not become a back door for market figures.

    The mart payload here has no tool calls, so its figure stays blocked — that
    deficiency belongs to the separate mart investigation, not to this change.
    """
    result = _mixed_result("문서에는 매출 수치가 없습니다.")
    answer = "## 시장 데이터\n\n리바로 매출은 83.18억원입니다."
    kept, report = N.enforce_numeric_copy_contract("q", answer, result)
    assert "83.18억원" not in kept
    assert report["disposition"] == "blocked"


def test_a_non_mixed_result_gains_no_extra_allowance():
    assert N._mixed_file_tokens({"tool_calls": []}) == ()
    assert N._mixed_file_tokens({"mixed_file_result": {"file_context": "   "}}) == ()


def test_file_tokens_are_read_from_the_deterministic_answer_too():
    tokens = N._mixed_file_tokens(
        {"mixed_file_result": {"deterministic_file_answer": "2026년 매출 1,200억원"}}
    )
    assert tokens
