from __future__ import annotations

from pathlib import Path

import pytest

from jw_chat_agent_poc.service import app as service_app
from jw_chat_agent_poc.service.app import SessionStore
from jw_chat_agent_poc.service.context_scope import ContextScope, resolve_context_scope
from jw_chat_agent_poc.service.genos_client import GenosClient
from jw_chat_agent_poc.service.runtime_provenance import trace_envelope
from jw_chat_agent_poc.tools.metrics.market_scope import MarketScopeResolver


FILE_CONTEXT = """[1] 리바로젯_프로젝트_검토.xlsx
시트: Summary, CrossTab
프로젝트 처리율: 37.8%
"""


class _ContaminatingAgent:
    def answer(self, question: str, _documents=None) -> dict:
        neutral = "승인코드" in question
        calls = [] if neutral else [{"tool": "get_brand_metric", "source": "cache", "render_data": {"brand": "리바로"}}]
        return {
            "answer": "파일 답변" if neutral else "파일 답변\n\n리바로 시장 꼬리",
            "sources": ["cache"],
            "tool_calls": calls,
        }


def _factory(*, external_mode: str = "live") -> _ContaminatingAgent:
    return _ContaminatingAgent()


def _resolver() -> MarketScopeResolver:
    return MarketScopeResolver()


@pytest.mark.parametrize(
    ("question", "documents"),
    (
        ("업로드 파일에서 리바로젯 프로젝트 처리율을 알려줘", []),
        ("업로드 자료에서 해당 프로젝트의 성장률 추이를 알려줘", []),
        ("업로드한 엑셀의 시트 구조와 교차표 항목을 알려줘", [Path("/tmp/crosstab.xlsx")]),
    ),
    ids=("A1_real_brand_overlap", "A2_default_brand_fallback", "A3_xlsx_open_question"),
)
def test_file_scope_blocks_market_tools_when_uploaded_context_is_active(question: str, documents: list[Path]) -> None:
    # Given: an active uploaded-file context whose real brand token overlaps the market router.
    # When: the file-directed question is answered.
    item = service_app._answer_question(
        SessionStore(),
        _resolver(),
        _factory,
        question,
        "live",
        None,
        documents=documents,
        file_context=FILE_CONTEXT,
    )

    # Then: server-side tool provenance is market-free and records FILE scope.
    result = item["result"]
    trace = trace_envelope(
        question=question,
        result=result,
        answer=str(result["answer"]),
        charts=[],
        timing={},
        conversation_id=item["conversation_id"],
    )
    assert trace["tools_called"] == []
    assert trace["scope"] == "FILE"
    assert "리바로 시장 꼬리" not in result["answer"]


def test_neutral_file_question_remains_market_free() -> None:
    # Given: an active file and a neutral file-directed question.
    question = "업로드 파일의 승인코드를 알려줘"
    # When: the request is answered.
    item = service_app._answer_question(
        SessionStore(), _resolver(), _factory, question, "live", None, file_context=FILE_CONTEXT
    )

    # Then: the existing uncontaminated baseline stays market-free.
    assert item["result"]["tool_calls"] == []


def test_file_session_market_question_keeps_market_path() -> None:
    # Given: a file exists, but the question explicitly asks for the market and does not reference the file.
    question = "리바로 시장점유율과 매출을 알려줘"
    # When: the request is answered.
    item = service_app._answer_question(
        SessionStore(), _resolver(), _factory, question, "live", None, file_context=FILE_CONTEXT
    )

    # Then: MARKET behavior is unchanged.
    assert [call["tool"] for call in item["result"]["tool_calls"]] == ["get_brand_metric"]


def test_explicit_mixed_question_keeps_both_sources_and_scope() -> None:
    # Given: an explicit request to compare an uploaded fact with the market.
    question = "업로드 파일의 처리율을 시장 평균과 비교해줘"
    # When: the request is answered.
    item = service_app._answer_question(
        SessionStore(), _resolver(), _factory, question, "live", None, file_context=FILE_CONTEXT
    )

    # Then: the market tool remains available and provenance records MIXED.
    result = item["result"]
    trace = trace_envelope(
        question=question,
        result=result,
        answer=str(result["answer"]),
        charts=[],
        timing={},
        conversation_id=item["conversation_id"],
    )
    assert [call["tool"] for call in result["tool_calls"]] == ["get_brand_metric"]
    assert "document" in result["sources"]
    assert trace["scope"] == "MIXED"


def test_question_without_file_context_is_unchanged() -> None:
    # Given: a normal market request without uploaded context.
    # When: it is answered.
    item = service_app._answer_question(
        SessionStore(), _resolver(), _factory, "리바로 시장점유율과 매출을 알려줘", "live", None
    )

    # Then: the market tool remains available.
    assert [call["tool"] for call in item["result"]["tool_calls"]] == ["get_brand_metric"]


@pytest.mark.parametrize(
    ("question", "active", "fresh", "market", "expected"),
    (
        ("리바로 시장점유율", False, False, True, ContextScope.MARKET),
        ("업로드 파일의 값", True, False, True, ContextScope.FILE),
        ("첨부 문서 요약", True, False, False, ContextScope.FILE),
        ("엑셀 시트 구조", True, False, True, ContextScope.FILE),
        ("처리율은?", True, True, True, ContextScope.FILE),
        ("처리율은?", True, False, False, ContextScope.FILE),
        ("리바로 시장점유율", True, False, True, ContextScope.MARKET),
        ("파일 값을 시장 평균과 비교", True, False, True, ContextScope.MIXED),
        ("문서 결과를 시장 데이터와 대비", True, False, True, ContextScope.MIXED),
        ("PDF 결과를 시장 기준으로 비교", True, False, True, ContextScope.MIXED),
    ),
)
def test_resolve_context_scope(
    question: str,
    active: bool,
    fresh: bool,
    market: bool,
    expected: ContextScope,
) -> None:
    assert resolve_context_scope(
        question,
        has_active_file=active,
        is_fresh_upload=fresh,
        has_market_intent=market,
    ) is expected


def test_mixed_scope_prompt_requires_separate_provenance_sections() -> None:
    messages = GenosClient._markdown_messages(
        "업로드 파일 값을 시장 평균과 비교",
        {"fact_md": "시장 값", "context_scope": "MIXED"},
        file_context="파일 값",
    )

    system = messages[0]["content"]
    assert "## 업로드 파일 기준" in system
    assert "## 시장 데이터 기준" in system
