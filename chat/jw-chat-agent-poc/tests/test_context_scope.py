from __future__ import annotations

from pathlib import Path
import threading

import pytest

from jw_chat_agent_poc.service import app as service_app
from jw_chat_agent_poc.service.app import SessionStore
from jw_chat_agent_poc.service.context_scope import ContextScope, resolve_context_scope
from jw_chat_agent_poc.service.genos_client import GenosClient
from jw_chat_agent_poc.service.file_search_client import UploadedFileSearchResult
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


def test_file_scope_stays_locked_when_search_context_times_out(monkeypatch) -> None:
    # Given: the session owns an uploaded document, but chunk search returned no context yet.
    monkeypatch.setattr(
        service_app,
        "search_uploaded_files",
        lambda question, conversation_id: UploadedFileSearchResult(
            file_context="",
            file_sources=(),
            errors=("search timeout",),
            has_active_file=True,
        ),
    )

    # When: a real market brand appears in a file-directed question.
    item = service_app._answer_question(
        SessionStore(),
        _resolver(),
        _factory,
        "업로드 파일에서 리바로젯 항목을 알려줘",
        "live",
        "ctx-with-file",
    )

    # Then: active-file ownership keeps the market path closed despite missing chunks.
    assert item["result"]["context_scope"] == "FILE"
    assert item["result"]["tool_calls"] == []
    assert "리바로 시장 꼬리" not in item["result"]["answer"]


def test_file_session_market_question_is_not_stuck_in_file_scope(monkeypatch) -> None:
    # Given: a file exists, but the question addresses only a market metric.
    question = "리바로 최근 매출 추이"
    monkeypatch.setattr(
        service_app,
        "_delegated_file_context",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("MARKET must not search files")),
    )
    # When: the request is answered.
    item = service_app._answer_question(
        SessionStore(), _resolver(), _factory, question, "live", None, file_context=FILE_CONTEXT
    )

    # Then: the active file does not capture the market-only request.
    assert item["result"]["context_scope"] == "MARKET"
    assert [call["tool"] for call in item["result"]["tool_calls"]] == ["get_brand_metric"]


def test_explicit_mixed_question_keeps_both_sources_and_scope() -> None:
    # Given: an explicit request to compare an uploaded fact with the market.
    question = "리바로 2025년 4월 매출과 이 보고서의 2026년 전망을 비교해줘"
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


def test_mixed_request_without_explicit_market_anchor_keeps_market_leg_closed() -> None:
    item = service_app._answer_question(
        SessionStore(),
        _resolver(),
        _factory,
        "보고서 전망이 최근 추이와 맞는지 확인해줘",
        "live",
        None,
        file_context=FILE_CONTEXT,
    )

    assert item["result"]["context_scope"] == "FILE"
    assert item["result"]["tool_calls"] == []
    assert "리바로 시장 꼬리" not in item["result"]["answer"]


def test_ambiguous_metric_request_with_active_file_asks_for_target(monkeypatch) -> None:
    monkeypatch.setattr(
        service_app,
        "_delegated_file_context",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("ambiguous scope must not search files")),
    )

    item = service_app._answer_question(
        SessionStore(),
        _resolver(),
        _factory,
        "매출 알려줘",
        "live",
        None,
        file_context=FILE_CONTEXT,
    )

    assert item["result"]["tool_calls"] == []
    assert "브랜드·시장" in item["result"]["answer"]


def test_verified_conversation_brand_anchor_escapes_file_scope() -> None:
    store = SessionStore()
    first = service_app._answer_question(
        store,
        _resolver(),
        _factory,
        "리바로 최근 매출 추이",
        "live",
        "anchored-market",
    )

    item = service_app._answer_question(
        store,
        _resolver(),
        _factory,
        "그 브랜드 최근 6개월 추이",
        "live",
        first["conversation_id"],
        file_context=FILE_CONTEXT,
    )

    assert item["result"]["context_scope"] == "MARKET"
    assert item["result"]["tool_calls"]


def test_mixed_legs_start_in_parallel(monkeypatch) -> None:
    both_started = threading.Barrier(2, timeout=1)

    monkeypatch.setattr(service_app, "has_active_uploaded_file", lambda _conversation_id: True)

    def file_leg(_question: str, _conversation_id: str | None, _file_context: str | None):
        both_started.wait()
        return FILE_CONTEXT, ({"file_name": "report.pdf"},), True, "파일 값"

    def market_leg(*_args, **_kwargs):
        both_started.wait()
        return _ContaminatingAgent().answer("리바로 매출")

    monkeypatch.setattr(service_app, "_delegated_file_context", file_leg)
    monkeypatch.setattr(service_app, "_answer_with_conversation", market_leg)

    item = service_app._answer_question(
        SessionStore(),
        _resolver(),
        _factory,
        "리바로 매출과 이 보고서 전망을 비교해줘",
        "live",
        "mixed-parallel",
    )

    assert item["result"]["context_scope"] == "MIXED"


def test_mixed_market_leg_exception_is_reported_as_partial_failure(monkeypatch) -> None:
    monkeypatch.setattr(service_app, "has_active_uploaded_file", lambda _conversation_id: True)
    monkeypatch.setattr(
        service_app,
        "_delegated_file_context",
        lambda *_args, **_kwargs: (
            FILE_CONTEXT,
            ({"file_name": "report.pdf"},),
            True,
            "파일 값",
        ),
    )
    monkeypatch.setattr(
        service_app,
        "_answer_with_conversation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("market leg failed")),
    )

    item = service_app._answer_question(
        SessionStore(),
        _resolver(),
        _factory,
        "리바로 매출과 이 보고서 전망을 비교해줘",
        "live",
        "mixed-exception",
    )

    assert item["result"]["context_scope"] == "MIXED"
    assert "조회 오류" in item["result"]["mixed_market_result"]["mixed_leg_error"]


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
        ("리바로 최근 매출 추이", True, False, True, ContextScope.MARKET),
        ("리바로 매출과 파일 값을 비교", True, False, True, ContextScope.MIXED),
        ("리바로 수치와 문서 결과를 시장 데이터로 대비", True, False, True, ContextScope.MIXED),
        ("리바로 실적과 PDF 결과를 시장 기준으로 비교", True, False, True, ContextScope.MIXED),
        ("리바로 매출과 이 보고서 전망을 비교", False, False, True, ContextScope.MARKET),
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
        has_market_anchor="리바로" in question,
    ) is expected


def test_mixed_scope_is_composed_without_synthesis_llm(monkeypatch) -> None:
    monkeypatch.setattr(
        GenosClient,
        "stream_answer",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("MIXED synthesis LLM must not run")),
    )
    final = service_app.compute_final_answer(
        "리바로 매출과 이 보고서 전망을 비교해줘",
        {
            "context_scope": "MIXED",
            "mixed_market_result": {
                "general_view_ready": True,
                "answer": "리바로 매출은 83.18억원입니다.\n\n| 출처 | 기준기간 | 뷰 | 시장정의 | 분모 | 채널 | 단위 |\n|---|---|---|---|---|---|---|\n| UBIST | 2025-04 | 전략뷰 | 리바로 시장 | 555개 | 전체 | 억원 |",
                "sources": ["UBIST"],
                "tool_calls": [],
                "markdown_response": {},
            },
            "mixed_file_result": {
                "answer": "",
                "sources": ["document"],
                "tool_calls": [],
                "file_context": "[1] report.pdf | p.7\n2026년 예상 매출 1,200억원",
                "file_source_items": [{"file_name": "report.pdf"}],
                "deterministic_file_answer": "2026년 예상 매출은 1,200억원입니다. (p.7)",
                "context_scope": "FILE",
            },
        },
        "mixed-final",
    )

    assert "## 시장 데이터" in final.text
    assert "## 첨부 문서 — report.pdf" in final.text
    assert "83.18억원" in final.text
    assert "1,200억원" in final.text
    assert "직접 비교" in final.text


def test_mixed_market_question_drops_file_clause() -> None:
    assert service_app._mixed_market_question(
        "리바로 2025년 4월 매출과 이 보고서의 2026년 전망을 비교해줘"
    ) == "리바로 2025년 4월 매출 알려줘"


def test_mixed_market_question_drops_leading_file_clause() -> None:
    assert service_app._mixed_market_question(
        "이 보고서의 2026년 전망과 리바로 2025년 4월 매출을 비교해줘"
    ) == "리바로 2025년 4월 매출을 알려줘"


def test_mixed_partial_failure_preserves_successful_file_leg(monkeypatch) -> None:
    monkeypatch.setattr(
        GenosClient,
        "stream_answer",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("partial MIXED must not run a synthesis LLM")
        ),
    )
    final = service_app.compute_final_answer(
        "리바로 매출과 이 보고서 전망을 비교해줘",
        {
            "context_scope": "MIXED",
            "mixed_market_result": {
                "mixed_leg_error": "시장 데이터 조회를 완료하지 못했습니다. 조회 오류입니다.",
            },
            "mixed_file_result": {
                "sources": ["document"],
                "file_context": "[1] report.pdf | p.7\n2026년 예상 매출 1,200억원",
                "file_source_items": [{"file_name": "report.pdf"}],
                "deterministic_file_answer": "2026년 예상 매출은 1,200억원입니다. (p.7)",
            },
        },
        "mixed-partial",
    )

    assert "시장 데이터 조회를 완료하지 못했습니다. 조회 오류입니다." in final.text
    assert "2026년 예상 매출은 1,200억원입니다. (p.7)" in final.text
    assert final.text.index("## 시장 데이터") < final.text.index("## 첨부 문서")


def test_mixed_finalization_exception_preserves_other_leg(monkeypatch) -> None:
    original = service_app._finalize_mixed_leg

    def finalize(leg: str, question: str, result: dict, conversation_id: str | None):
        if leg == "market":
            raise RuntimeError("market rendering failed")
        return original(leg, question, result, conversation_id)

    monkeypatch.setattr(service_app, "_finalize_mixed_leg", finalize)
    final = service_app.compute_final_answer(
        "리바로 매출과 이 보고서 전망을 비교해줘",
        {
            "context_scope": "MIXED",
            "mixed_market_result": {"answer": "시장 값"},
            "mixed_file_result": {
                "sources": ["document"],
                "file_context": "[1] report.pdf | p.7\n2026년 예상 매출 1,200억원",
                "file_source_items": [{"file_name": "report.pdf"}],
                "deterministic_file_answer": "2026년 예상 매출은 1,200억원입니다. (p.7)",
            },
        },
        "mixed-final-exception",
    )

    assert "시장 데이터 조회를 완료하지 못했습니다. 조회 오류입니다." in final.text
    assert "2026년 예상 매출은 1,200억원입니다. (p.7)" in final.text


def test_mixed_finalization_uses_a_fresh_request_deadline(monkeypatch) -> None:
    monkeypatch.setenv("JW_CHAT_MIXED_TOTAL_TIMEOUT_S", "1")

    def finalize(leg: str, _question: str, _result: dict, conversation_id: str | None):
        threading.Event().wait(0.02)
        return service_app.FinalAnswer(
            text="시장 값" if leg == "market" else "파일 값",
            charts=(),
            timing={},
            trace={},
            sources=(),
            conversation_id=conversation_id,
            file_sources=(),
        )

    monkeypatch.setattr(service_app, "_finalize_mixed_leg", finalize)
    final = service_app.compute_final_answer(
        "리바로 매출과 이 보고서 전망을 비교해줘",
        {
            "context_scope": "MIXED",
            "mixed_started_monotonic": 1.0,
            "mixed_market_result": {"answer": "시장 값"},
            "mixed_file_result": {
                "answer": "파일 값",
                "file_source_items": [{"file_name": "report.pdf"}],
            },
        },
        "mixed-stale-result",
    )

    assert "시장 값" in final.text
    assert "파일 값" in final.text
    assert "처리 시간을 초과" not in final.text
