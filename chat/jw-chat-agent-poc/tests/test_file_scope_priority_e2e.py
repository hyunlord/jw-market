from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from jw_chat_agent_poc.service import app as service_app
from jw_chat_agent_poc.service.app import SessionStore, create_app
from jw_chat_agent_poc.service.file_search_client import UploadedFileSearchResult
from jw_chat_agent_poc.tools.metrics.market_scope import MarketScopeResolver


SCHEMA_COLUMNS = ("ATC 4", "MFR NAME KOR", "VALUES LC SI PRICE 1/2026")


class _MarketAgent:
    def __init__(self, *, external_mode: str = "live") -> None:
        self.external_mode = external_mode

    def answer(self, question: str, _documents=None) -> dict:
        return {
            "answer": "리바로 2025-04 매출은 83.184115억원입니다.",
            "sources": ["UBIST"],
            "tool_calls": [{"tool": "get_brand_metric", "render_data": {"brand": "리바로"}}],
        }


def _market_factory(*, external_mode: str = "live") -> _MarketAgent:
    return _MarketAgent(external_mode=external_mode)


def _uploaded(answer: str) -> UploadedFileSearchResult:
    return UploadedFileSearchResult(
        file_context="## 업로드 파일 SQL 결과\n상태: 확인됨\n" + answer,
        file_sources=("fixture.xlsx",),
        errors=(),
        file_source_items=({"file_name": "fixture.xlsx"},),
        deterministic_answer=answer,
        sql_trace=(
            {"stage": "schema", "status": "ok", "table_count": "1"},
            {"stage": "execution", "status": "ok"},
        ),
    )


def _post_and_result(client: TestClient, store: SessionStore, question: str, conversation_id: str) -> dict:
    response = client.post(
        "/chat",
        json={"question": question, "conversation_id": conversation_id},
    )
    assert response.status_code == 200
    stored = store.get(response.json()["session_id"])
    assert stored is not None
    return stored["result"]


def _file_client(
    monkeypatch,
    answers: dict[str, str],
    *,
    schema_columns: tuple[str, ...] = SCHEMA_COLUMNS,
) -> tuple[TestClient, SessionStore, list[str]]:
    calls: list[str] = []
    monkeypatch.setattr(service_app, "has_active_uploaded_file", lambda _conversation_id: True)
    monkeypatch.setattr(
        service_app,
        "fetch_uploaded_file_schema_columns",
        lambda _conversation_id: schema_columns,
    )

    def search(question: str, _conversation_id: str | None) -> UploadedFileSearchResult:
        calls.append(question)
        return _uploaded(answers[question])

    monkeypatch.setattr(service_app, "search_uploaded_files", search)
    store = SessionStore()
    return TestClient(create_app(agent_factory=_market_factory, store=store)), store, calls


@pytest.mark.parametrize(
    ("question", "columns", "answer"),
    (
        ("채널별 건수", ("CHANNEL", "응답자 번호"), "온라인 10건 / 병원 7건"),
        (
            "상위 10개 제품",
            ("PRODUCT NAME KOR", "VALUES LC SI PRICE 1/2026"),
            "상위 10개 제품 집계",
        ),
    ),
)
def test_chat_route_uses_file_schema_axis_before_scope_clarification(
    monkeypatch,
    question: str,
    columns: tuple[str, ...],
    answer: str,
) -> None:
    client, store, calls = _file_client(
        monkeypatch,
        {question: answer},
        schema_columns=columns,
    )

    result = _post_and_result(client, store, question, f"schema-axis-{question}")

    assert result["context_scope"] == "FILE"
    assert result["deterministic_file_answer"] == answer
    assert calls == [question]


@pytest.mark.parametrize(
    ("question", "answer", "expected"),
    (
        ("2026년 1월 총 sell-out 금액은?", "총액 386,933,825,518원", "386,933,825,518"),
        ("동아제약의 sell-out 합계는?", "동아제약 21,978,584,141원", "21,978,584,141"),
        ("동화약품의 합계는?", "동화약품 15,188,575,523원", "15,188,575,523"),
    ),
)
def test_chat_route_preserves_verified_file_aggregates(
    monkeypatch,
    question: str,
    answer: str,
    expected: str,
) -> None:
    client, store, calls = _file_client(monkeypatch, {question: answer})

    result = _post_and_result(client, store, question, f"aggregate-{expected}")

    assert result["context_scope"] == "FILE"
    assert expected in result["deterministic_file_answer"]
    assert calls == [question]
    assert result["tool_calls"] == []


def test_chat_route_keeps_file_comparison_out_of_market_contract(monkeypatch) -> None:
    question = "동아제약과 동화약품 비교"
    answer = "동아제약과 동화약품의 차이는 6,790,008,618원입니다."
    client, store, calls = _file_client(monkeypatch, {question: answer})

    result = _post_and_result(client, store, question, "scope-comparison")

    assert result["context_scope"] == "FILE"
    assert "6,790,008,618" in result["deterministic_file_answer"]
    assert "시장 도구" not in result["deterministic_file_answer"]
    assert calls == [question]
    assert result["tool_calls"] == []


def test_chat_route_prefers_file_schema_for_atc4_comparison(monkeypatch) -> None:
    # Given: a real /chat request in a session with a SQL workbook.
    question = "R05A0에서 동아제약과 동화약품 비교"
    answer = "동화약품 3,853,883,875원 / 동아제약 3,315,233,364원"
    calls: list[str] = []
    monkeypatch.setattr(service_app, "has_active_uploaded_file", lambda _conversation_id: True)
    monkeypatch.setattr(
        service_app,
        "fetch_uploaded_file_schema_columns",
        lambda _conversation_id: SCHEMA_COLUMNS,
    )

    def search(search_question: str, _conversation_id: str | None) -> UploadedFileSearchResult:
        calls.append(search_question)
        return _uploaded(answer)

    monkeypatch.setattr(service_app, "search_uploaded_files", search)
    store = SessionStore()
    client = TestClient(create_app(agent_factory=_market_factory, store=store))

    # When: the request passes through the public /chat endpoint.
    result = _post_and_result(client, store, question, "scope-atc4")

    # Then: scope and orchestration both stay on the uploaded-file SQL leg.
    assert result["context_scope"] == "FILE"
    assert result["deterministic_file_answer"] == answer
    assert calls == [question]
    assert result["tool_calls"] == []


def test_chat_route_preserves_market_scope_for_unrelated_brand(monkeypatch) -> None:
    # Given: the same workbook schema lacks a Livalo brand axis.
    monkeypatch.setattr(service_app, "has_active_uploaded_file", lambda _conversation_id: True)
    monkeypatch.setattr(
        service_app,
        "fetch_uploaded_file_schema_columns",
        lambda _conversation_id: SCHEMA_COLUMNS,
    )
    monkeypatch.setattr(
        service_app,
        "search_uploaded_files",
        lambda *_args: (_ for _ in ()).throw(AssertionError("MARKET must not run file SQL")),
    )
    store = SessionStore()
    client = TestClient(
        create_app(
            agent_factory=_market_factory,
            market_scope_resolver=MarketScopeResolver(),
            store=store,
        )
    )

    # When: a market-only question is sent through /chat.
    result = _post_and_result(client, store, "리바로 최근 매출 추이", "scope-market")

    # Then: G2 remains MARKET and uses the market metric tool.
    assert result["context_scope"] == "MARKET"
    assert [call["tool"] for call in result["tool_calls"]] == ["get_brand_metric"]


def test_chat_route_preserves_unsupported_measure_fail_closed(monkeypatch) -> None:
    question = "2035년 재구매율 합계"
    answer = "이 파일에는 재구매율 관련 열이 없습니다."
    client, store, _calls = _file_client(monkeypatch, {question: answer})

    result = _post_and_result(client, store, question, "unsupported-measure")

    assert result["context_scope"] == "FILE"
    assert result["deterministic_file_answer"] == answer
    assert "386,933,825,518" not in result["deterministic_file_answer"]


def test_chat_route_preserves_file_multiturn_subject(monkeypatch) -> None:
    first = "동아제약 합계"
    follow_up = "동화약품은?"
    resolved_follow_up = "동화약품의 합계는?"
    client, store, calls = _file_client(
        monkeypatch,
        {
            first: "동아제약 21,978,584,141원",
            resolved_follow_up: "동화약품 15,188,575,523원",
        },
    )

    _post_and_result(client, store, first, "file-multiturn")
    result = _post_and_result(client, store, follow_up, "file-multiturn")

    assert result["context_scope"] == "FILE"
    assert "15,188,575,523" in result["deterministic_file_answer"]
    assert calls == [first, resolved_follow_up]


def test_chat_route_does_not_turn_ambiguous_analysis_into_inherited_sum(monkeypatch) -> None:
    first = "동아제약 합계"
    broad = "분석해줘"
    clarification = "어떤 기준으로 분석할까요? 제조사별 합계나 월별 추이를 골라 주세요."
    client, store, calls = _file_client(
        monkeypatch,
        {
            first: "동아제약 21,978,584,141원",
            broad: clarification,
        },
    )

    _post_and_result(client, store, first, "file-ambiguous-followup")
    result = _post_and_result(client, store, broad, "file-ambiguous-followup")

    assert result["context_scope"] == "FILE"
    assert result["deterministic_file_answer"] == clarification
    assert calls == [first, broad]


def test_chat_route_preserves_bpi_deterministic_result(monkeypatch) -> None:
    question = "q1별 응답 수와 no 합계"
    answer = "q1=1: 690 / 2,679,529, q1=2: 910 / 2,555,501"
    client, store, calls = _file_client(monkeypatch, {question: answer})

    result = _post_and_result(client, store, question, "bpi")

    assert result["context_scope"] == "FILE"
    assert "690 / 2,679,529" in result["deterministic_file_answer"]
    assert "910 / 2,555,501" in result["deterministic_file_answer"]
    assert calls == [question]


def test_chat_route_treats_explicit_bpi_sheet_as_file_reference(monkeypatch) -> None:
    question = "Numeric 시트에서 q1=1.0과 q1=2.0 각각의 응답자 수와 no 합계를 표로 알려줘."
    answer = "q1=1.0: 690 / 2,679,529, q1=2.0: 910 / 2,555,501"
    calls: list[str] = []
    monkeypatch.setattr(service_app, "has_active_uploaded_file", lambda _conversation_id: True)
    monkeypatch.setattr(
        service_app,
        "fetch_uploaded_file_schema_columns",
        lambda _conversation_id: ("q1", "no"),
    )

    def search(search_question: str, _conversation_id: str | None) -> UploadedFileSearchResult:
        calls.append(search_question)
        return _uploaded(answer)

    monkeypatch.setattr(service_app, "search_uploaded_files", search)
    store = SessionStore()
    client = TestClient(create_app(agent_factory=_market_factory, store=store))

    result = _post_and_result(client, store, question, "bpi-explicit-sheet")

    assert result["context_scope"] == "FILE"
    assert result["deterministic_file_answer"] == answer
    assert calls == [question]


def test_chat_route_period_phrase_reaches_market_parser(monkeypatch) -> None:
    monkeypatch.setattr(service_app, "has_active_uploaded_file", lambda _conversation_id: False)
    store = SessionStore()
    client = TestClient(create_app(agent_factory=_market_factory, store=store))

    result = _post_and_result(client, store, "리바로 2025년 4월 매출", "market-period")

    assert result["context_scope"] == "MARKET"
    assert "83.184115" in result["answer"]


def test_chat_route_file_summary_remains_isolated(monkeypatch) -> None:
    question = "이 보고서 요약해줘"
    client, store, _calls = _file_client(monkeypatch, {question: "보고서 요약"})

    result = _post_and_result(client, store, question, "file-summary")

    assert result["context_scope"] == "FILE"
    assert result["tool_calls"] == []


def test_chat_route_without_market_anchor_does_not_invent_brand(monkeypatch) -> None:
    question = "보고서 전망이 최근 추이와 맞는지 확인해줘"
    client, store, _calls = _file_client(monkeypatch, {question: "시장 기준이 없어 파일만 확인했습니다."})

    result = _post_and_result(client, store, question, "no-market-anchor")

    assert result["context_scope"] == "FILE"
    assert result["tool_calls"] == []
    assert "리바로" not in result["deterministic_file_answer"]
