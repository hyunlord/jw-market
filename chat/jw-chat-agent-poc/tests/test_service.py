from __future__ import annotations

import json
from pathlib import Path
import pytest
import requests
from types import SimpleNamespace

from fastapi.testclient import TestClient

from jw_chat_agent_poc.orchestrator.markdown_response import MarkdownResponseBuilder
from jw_chat_agent_poc.service.answer_safety import chunk_text, ensure_file_page_evidence
from jw_chat_agent_poc.service import app as service_app
from jw_chat_agent_poc.service.genos_client import GenosClient
from jw_chat_agent_poc.service.app import SessionStore, _sse_delta, compute_final_answer, create_app
from jw_chat_agent_poc.service.conversation import ConversationSlots, PendingClarification
from jw_chat_agent_poc.service.runtime_provenance import trace_envelope
from jw_chat_agent_poc.service.sse_protocol import iter_markdown_sse_events
from jw_chat_agent_poc.tools.metrics.cache_live import StaticCausePayloadReader, StaticMetricsCacheReader
from jw_chat_agent_poc.tools.metrics.market_scope import MarketScopeResolver
from jw_chat_agent_poc.tools.query_layer import MartRecord, StaticStrategicMartReader, StrategicQueryLayer
from jw_chat_agent_poc.resolver import UnsupportedBrandError
from jw_chat_agent_poc.router import BQRouter

from test_metrics_cache import BRAND_CARDS, CACHE_BRANDS, CAUSE_PAYLOAD


class FakeAgent:
    calls: list[tuple[str, str]] = []

    def __init__(self, *, external_mode: str = "live") -> None:
        self.external_mode = external_mode

    def answer(self, question: str, _documents=None) -> dict:
        self.calls.append((question, self.external_mode))
        return {
            "answer": f"fallback:{question}",
            "sources": ["cache"],
            "tool_calls": [],
        }


def _fake_agent_factory(*, external_mode: str = "live") -> FakeAgent:
    return FakeAgent(external_mode=external_mode)


def _market_scope_resolver() -> MarketScopeResolver:
    cache_reader = StaticMetricsCacheReader(cache_brands=CACHE_BRANDS, market_status=BRAND_CARDS)
    cause_reader = StaticCausePayloadReader(
        {
            ("리바로", "market_landscape", "UBIST", "sales", "strategy_006"): CAUSE_PAYLOAD,
        }
    )
    return MarketScopeResolver(cache_reader=cache_reader, cause_reader=cause_reader)


def test_market_scope_queries_explicit_strategy_id_without_brand_fallback() -> None:
    def record(brand: str, value: float) -> MartRecord:
        return MartRecord(
            ml_id="ml_006",
            brand_name=brand,
            source="ubist",
            measure="sales",
            metric_history={"2025-04": {"raw_value": value}},
            channel_data={},
            specialty_data={},
            dimension_data={},
            by_dimension={},
        )

    resolver = MarketScopeResolver(
        cache_reader=StaticMetricsCacheReader(cache_brands=CACHE_BRANDS, market_status={}),
        query_layer=StrategicQueryLayer(
            reader=StaticStrategicMartReader(
                (record("리바로", 8_318_411_500.0), record("리바로젯", 9_781_370_500.0))
            )
        ),
    )

    result = resolver.answer_market_id("ml_006 2025-04 시장규모", market_id="ml_006", period="2025-04")

    data = result["tool_calls"][0]["render_data"]
    assert data["market_id"] == "ml_006"
    assert data["period"] == "2025-04"
    assert data["market_size_recent_krw"] == 18_099_782_000.0
    assert data["market_size_억원"] == 180.99782


def _reconstruct_answer_from_sse(sse: str) -> str:
    answer_parts: list[str] = []
    for block in sse.split("\n\n"):
        if block.startswith("event: delta\n"):
            answer_parts.append("\n".join(line.removeprefix("data: ") for line in block.splitlines()[1:]))
        elif block.startswith("event: markdown_block\n"):
            payload = json.loads(block.split("data: ", 1)[1])
            answer_parts.append(payload["markdown"])
    return "".join(answer_parts)


def _normalize_markdown_spacing(markdown: str) -> str:
    return "\n\n".join(part.strip() for part in markdown.strip().split("\n\n") if part.strip())


def test_compute_final_answer_appends_blocked_metric_notice(monkeypatch) -> None:
    response = MarkdownResponseBuilder().build(
        brand="악템라",
        calls=[
            {
                "tool": "get_brand_metric",
                "source": "cache",
                "render_data": {
                    "brand": "악템라",
                    "metric": "sales",
                    "period": "2025-Q4",
                    "requested_period": "2026-04",
                    "fallback_period": "2025-Q4",
                    "sales_억원": 48.19,
                    "ms_recent_pct": 4.34,
                    "rank": 8,
                    "total_brands_in_market": 26,
                    "source_status": "OK",
                    "blocked_metric_values": [
                        {
                            "period": "2026-04",
                            "status": "query_failed",
                            "message": "2026-04 값은 조회 실패/시장 매핑 불완전으로 표시하지 않습니다.",
                        }
                    ],
                },
            }
        ],
        sources=["cache"],
    )

    def stream_answer(_self: GenosClient, _question: str, _result: dict):
        yield "악템라는 사용 가능한 최신 기준 2025-Q4 매출 48.19억원, MS 4.34%입니다."

    monkeypatch.setattr(GenosClient, "stream_answer", stream_answer)
    final = compute_final_answer(
        "악템라 2026-04 매출 알려줘",
        {"answer": "", "markdown_response": response.to_dict(), "tool_calls": [], "sources": ["cache"]},
        "test-conversation",
    )

    notice = "2026-04 값은 조회 실패/시장 매핑 불완전으로 표시하지 않습니다."
    assert final.text.count(notice) == 1
    assert "48.19억원" in final.text
    assert "4.34%" in final.text
    assert "0.00억원" not in final.text
    assert "23/26" not in final.text


def test_compute_final_answer_replaces_internal_csd_facts_for_general_view_ready() -> None:
    fact_md = """### 필수 답변 fact
| 구분 | 반드시 반영할 내용 |
| --- | --- |
| CSD aggregate 콜수 | 리바로 CSD ChannelDynamics aggregate 콜수/활동량 2026-03 120건 → 2026-04 135건 |
| CSD 세부 미지원 | impact level, HCP/의사별, 기관별 |
"""
    leaked = """요청한 값은 현재 조회 결과에 존재합니다.

## 확정 데이터

| 구분 | 반드시 반영할 내용 |
| --- | --- |
| CSD aggregate 콜수 | 리바로 CSD ChannelDynamics aggregate 콜수/활동량 2026-03 120건 → 2026-04 135건 |
| CSD 세부 미지원 | impact level, HCP/의사별, 기관별 |
"""

    final = compute_final_answer(
        "리바로 영업활동 추이 어때?",
        {
            "general_view_ready": True,
            "answer": leaked,
            "markdown_response": {"fact_md": fact_md},
            "sources": ["cache"],
        },
        "test-conversation",
    )

    assert "2026-03 120건" in final.text
    assert "2026-04 135건" in final.text
    assert "영업활동" in final.text
    assert "확정 데이터" not in final.text
    assert "반드시 반영할 내용" not in final.text
    assert "CSD 세부 미지원" not in final.text


def test_compute_final_answer_replaces_internal_csd_facts_after_agent_loop(monkeypatch) -> None:
    fact_md = """### 필수 답변 fact
| 구분 | 반드시 반영할 내용 |
| --- | --- |
| Brand 상위 | 1위 로수젯 시장점유율 9.13% 매출 195.24억원 |
| CSD aggregate 콜수 | 리바로 CSD ChannelDynamics aggregate 콜수/활동량 2025-06 1,775건 → 2026-05 1,769건 |
| CSD 세부 미지원 | impact level, HCP/의사별, 기관별 |
"""
    leaked = """요청한 값은 현재 조회 결과에 존재합니다.

## 확정 데이터

| CSD aggregate 콜수 | 리바로 CSD ChannelDynamics aggregate 콜수/활동량 2025-06 1,775건 → 2026-05 1,769건 |
| CSD 세부 미지원 | impact level, HCP/의사별, 기관별 |
"""

    def stream_answer(_self: GenosClient, _question: str, _result: dict):
        yield leaked

    monkeypatch.setattr(GenosClient, "stream_answer", stream_answer)
    final = compute_final_answer(
        "리바로 영업활동 추이 어때?",
        {
            "answer": "",
            "markdown_response": {"fact_md": fact_md},
            "tool_calls": [
                {"tool": "csd_activity_trend", "render_data": {"status": "ok"}},
                {"tool": "get_brand_metric", "render_data": {"status": "ok"}},
            ],
            "sources": ["CSD ChannelDynamics"],
        },
        "test-conversation",
    )

    assert "2025-06 1,775건" in final.text
    assert "2026-05 1,769건" in final.text
    assert "영업활동" in final.text
    assert "확정 데이터" not in final.text
    assert "CSD aggregate 콜수" not in final.text
    assert "CSD 세부 미지원" not in final.text


def test_compute_final_answer_keeps_natural_competition_lead_after_all_contracts(monkeypatch) -> None:
    fact_md = """### 필수 답변 fact
| 구분 | 반드시 반영할 내용 |
| --- | --- |
| Brand 상위 | 1위 로수젯 시장점유율 9.13% 매출 195.24억원 |
| Brand 상위 | 2위 리피토 시장점유율 6.13% 매출 131.09억원 |
| Brand 상위 | 3위 리바로젯 시장점유율 5.12% 매출 109.46억원 |
"""
    generated = """구체적으로는 로수젯 시장점유율 9.13%, 매출 195.24억원입니다.

| 순위 | 브랜드 | 점유율 | 매출 |
| --- | --- | --- | --- |
| 1위 | 로수젯 | 9.13% | 195.24억원 |
| 2위 | 리피토 | 6.13% | 131.09억원 |
| 3위 | 리바로젯 | 5.12% | 109.46억원 |
"""

    monkeypatch.setattr(GenosClient, "stream_answer", lambda *_args: iter((generated,)))
    final = compute_final_answer(
        "리바로 경쟁구도 어떻게 변하고 있어",
        {
            "answer": "",
            "markdown_response": {"fact_md": fact_md},
            "tool_calls": [{"tool": "get_top_brands", "render_data": {"status": "ok"}}],
            "sources": ["UBIST"],
        },
        "natural-competition",
    )

    assert final.text.startswith("리바로 경쟁구도를 보면 로수젯이 9.13%(195.24억원)로 선두이며")
    assert "| 1위 | 로수젯 | 9.13% | 195.24억원 |" in final.text


def test_answer_question_directs_agent_loop_without_chat_agent_facade(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Resolver:
        def resolve(self, question: str, *, allow_default: bool = False):
            captured["resolved"] = (question, allow_default)
            return SimpleNamespace(canonical_brand="리바로")

    class Dependencies:
        router = BQRouter()
        resolver = Resolver()

        def agent_loop_dependencies(self):
            captured["loop_dependencies"] = True
            return "loop-deps"

    class Loop:
        def answer(self, question: str) -> dict:
            captured["loop_question"] = question
            return {"answer": "direct-loop", "sources": ["cache"], "tool_calls": []}

    def build_deps(*, external_mode: str = "fixture") -> Dependencies:
        captured["external_mode"] = external_mode
        return Dependencies()

    def build_loop(dependencies):
        captured["built_with"] = dependencies
        return Loop()

    def fail_factory(*, external_mode: str = "live"):
        raise AssertionError("ChatAgent facade should be bypassed for default agent-loop questions")

    monkeypatch.setattr(service_app, "build_chat_agent_dependencies", build_deps)
    monkeypatch.setattr(service_app, "build_tool_use_agent", build_loop)

    item = service_app._answer_question(
        SessionStore(),
        _market_scope_resolver(),
        fail_factory,
        "리바로 경쟁 구도 변화",
        "live",
        None,
        use_direct_agent_loop=True,
    )

    assert item["result"]["answer"] == "direct-loop"
    assert captured["external_mode"] == "live"
    assert captured["resolved"] == ("리바로 경쟁 구도 변화", False)
    assert captured["built_with"] == "loop-deps"
    assert captured["loop_question"] == "리바로 경쟁 구도 변화"


def test_answer_question_source_trap_uses_chat_agent_facade_before_direct_agent_loop(monkeypatch) -> None:
    def fail_build_loop(_dependencies):
        raise AssertionError("requested-source trap must not enter direct agent loop")

    monkeypatch.setattr(service_app, "build_tool_use_agent", fail_build_loop)
    FakeAgent.calls.clear()

    item = service_app._answer_question(
        SessionStore(),
        _market_scope_resolver(),
        _fake_agent_factory,
        "리바로 KOL 자문 기준 처방 의견과 시장 시사점을 알려줘",
        "live",
        None,
        use_direct_agent_loop=True,
    )

    assert item["result"]["answer"].startswith("fallback:리바로 KOL 자문")
    assert FakeAgent.calls == [("리바로 KOL 자문 기준 처방 의견과 시장 시사점을 알려줘", "live")]


def test_answer_question_external_contract_uses_chat_agent_facade_before_direct_agent_loop(monkeypatch) -> None:
    captured: list[tuple[str, str]] = []

    class ExternalContractAgent:
        def answer(self, question: str, _documents=None) -> dict:
            captured.append((question, "answered"))
            return {
                "answer": "verified external evidence",
                "sources": ["external"],
                "tool_calls": [{"tool": "clinicaltrials_v2_search"}],
                "router_diagnostics": {"mode": "tool_use_agent"},
            }

    def factory(*, external_mode: str = "live") -> ExternalContractAgent:
        captured.append((external_mode, "factory"))
        return ExternalContractAgent()

    monkeypatch.setenv("CHAT_EXTERNAL_TOOL_AGENT_ENABLED", "1")
    monkeypatch.setattr(
        service_app,
        "_answer_direct_agent_loop",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("external structural contracts must not bypass the ChatAgent facade")
        ),
    )

    questions = (
        "리바로 임상시험",
        "리바로 특허 만료일",
        "고지혈증 질환(성분)의 임상·허가심사 단계 경쟁약물 현황을 알려줘 .",
    )
    for question in questions:
        item = service_app._answer_question(
            SessionStore(),
            _market_scope_resolver(),
            factory,
            question,
            "live",
            None,
            use_direct_agent_loop=True,
        )

        assert item["result"]["router_diagnostics"]["mode"] == "tool_use_agent"

    assert captured == [
        ("live", "factory"),
        ("리바로 임상시험", "answered"),
        ("live", "factory"),
        ("리바로 특허 만료일", "answered"),
        ("live", "factory"),
        (questions[2], "answered"),
    ]


@pytest.mark.parametrize(
    ("question", "tool", "evidence_text"),
    (
        ("리바로 임상시험", "clinicaltrials_v2_search", "NCT05537948 임상시험 근거"),
        ("리바로 특허 만료일", "mfds_patent", "국내 특허 10-0830018 근거"),
        ("pitavastatin 안전성", "openfda_label_search", "FDA 라벨 이상반응 근거"),
    ),
)
def test_compute_final_answer_preserves_verified_tool_use_evidence(
    question: str,
    tool: str,
    evidence_text: str,
) -> None:
    result = {
        "answer": evidence_text,
        "sources": ["verified external source"],
        "tool_calls": [
            {
                "tool": tool,
                "status": "ok",
                "render_data": {
                    "ok": True,
                    "evidence": [{"source_name": "verified external source"}],
                },
            }
        ],
        "markdown_response": {"fact_md": evidence_text, "allowed_numbers": ()},
        "router_diagnostics": {"mode": "tool_use_agent"},
    }

    final = compute_final_answer(question, result, "tool-use-final")

    assert evidence_text in final.text
    assert "필요 도구" not in final.text
    assert "현재 확인 불가" not in final.text


def test_answer_question_locks_fresh_document_questions_to_file_scope(monkeypatch) -> None:
    def fail_direct_dependencies(*, external_mode: str = "fixture"):
        raise AssertionError("document questions must keep the ChatAgent/RAG facade")

    monkeypatch.setattr(service_app, "build_chat_agent_dependencies", fail_direct_dependencies)
    FakeAgent.calls = []

    item = service_app._answer_question(
        SessionStore(),
        _market_scope_resolver(),
        _fake_agent_factory,
        "이 문서의 리바로 경쟁 구도 변화",
        "live",
        None,
        documents=[Path("/tmp/example.pdf")],
        use_direct_agent_loop=True,
    )

    assert item["result"]["answer"] == "업로드 파일에서 확인된 근거만 사용해 답변합니다."
    assert item["result"]["tool_calls"] == []
    assert item["result"]["context_scope"] == "FILE"
    assert FakeAgent.calls == []


def test_answer_question_returns_deterministic_file_only_ready_without_agent() -> None:
    def fail_factory(*, external_mode: str = "live"):
        raise AssertionError("file-only empty questions must not enter embedding or planner paths")

    item = service_app._answer_question(
        SessionStore(),
        _market_scope_resolver(),
        fail_factory,
        "   ",
        "live",
        None,
        documents=[Path("/tmp/A.pdf"), Path("/tmp/B.xlsx")],
        use_direct_agent_loop=True,
    )

    result = item["result"]
    assert result["file_only_ready"] is True
    assert result["file_names"] == ["A.pdf", "B.xlsx"]
    assert "파일 2개 저장 완료" in result["answer"]
    assert "A.pdf" in result["answer"]
    assert "B.xlsx" in result["answer"]


def test_chat_rejects_empty_question_without_files() -> None:
    client = TestClient(create_app(agent_factory=_fake_agent_factory))

    response = client.post("/chat", json={"question": "   "})

    assert response.status_code == 400
    assert response.json()["detail"] == "질문 또는 파일 업로드가 필요합니다."


def test_chat_answer_accepts_empty_question_when_files_exist(monkeypatch) -> None:
    def fail_factory(*, external_mode: str = "live"):
        raise AssertionError("file-only empty questions must not call the agent")

    def fail_stream(*_args, **_kwargs):
        raise AssertionError("file-only ready message must not call GenOS final synthesis")

    monkeypatch.setattr(GenosClient, "_stream_chat", fail_stream)
    client = TestClient(create_app(agent_factory=fail_factory))

    response = client.post(
        "/chat/answer",
        json={"question": "   ", "document_paths": ["/tmp/A.pdf", "/tmp/B.xlsx"]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["sources"] == ["file_upload"]
    assert "파일 2개 저장 완료" in body["text"]
    assert "A.pdf" in body["text"]
    assert "B.xlsx" in body["text"]


def test_chat_answer_attaches_file_context_as_document_source(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class AgentWithBasicResult:
        def __init__(self, *, external_mode: str = "live") -> None:
            self.external_mode = external_mode

        def answer(self, _question: str, _documents=None) -> dict:
            return {
                "answer": "fallback",
                "sources": ["cache"],
                "tool_calls": [],
            }

    def stream_answer(_self: GenosClient, question: str, result: dict):
        captured["question"] = question
        captured["result"] = result
        yield "확정 데이터 기준으로 정리하면 다음과 같습니다.\n\n- 표시할 검증 fact가 제한적입니다."

    monkeypatch.setattr(GenosClient, "stream_answer", stream_answer)
    app = create_app(agent_factory=lambda external_mode="live": AgentWithBasicResult(external_mode=external_mode))
    client = TestClient(app)

    response = client.post(
        "/chat/answer",
        json={
            "question": "업로드 파일에서 CodexA 값을 알려줘",
            "file_context": "파일: sample.xlsx\nCodexA=123.45",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert "CodexA=123.45" in body["text"]
    assert "| 업로드 파일(sample.xlsx) | \u2014 | 파일 | \u2014 | \u2014 | 전체 | \u2014 |" in body["text"]
    result = captured["result"]
    assert isinstance(result, dict)
    assert result["file_context"] == "파일: sample.xlsx\nCodexA=123.45"
    assert result["tool_calls"] == []
    assert "document" in result["sources"]
    assert body["sources"] == ["document"]


def test_genos_final_answer_uses_uploaded_file_context_numbers(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def chat_text(self: GenosClient, messages: list[dict[str, str]]) -> str:
        captured["messages"] = messages
        return "업로드 파일 기준 CodexA 값은 123.45입니다."

    monkeypatch.setattr(GenosClient, "_chat_text", chat_text)
    client = GenosClient(base_url="http://unused", token="token")
    result = {
        "answer": "fallback",
        "sources": ["cache", "document"],
        "tool_calls": [],
        "markdown_response": {"fact_md": "", "allowed_numbers": ()},
        "file_context": "업로드 파일 sample.xlsx 검색 결과: CodexA 값 123.45",
    }

    text = "".join(client.stream_answer("업로드 파일에서 CodexA 값을 알려줘", result))

    assert "123.45" in text
    assert "| 업로드 파일(sample.xlsx) | — | 파일 | — | — | 전체 | — |" in text
    messages = captured["messages"]
    assert isinstance(messages, list)
    assert "업로드 파일 컨텍스트" in messages[1]["content"]
    assert "CodexA 값 123.45" in messages[1]["content"]


def test_deterministic_file_aggregate_bypasses_final_llm(monkeypatch) -> None:
    monkeypatch.setattr(
        GenosClient,
        "stream_answer",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("LLM must not run")),
    )
    answer = (
        "## 업로드 파일 집계 결과\n"
        "파일: CHSO.xlsx\n"
        "시트·테이블명: Basic / data\n"
        "필터 조건: 전체 행\n"
        "사용 열: 1/2026 VALUES LC SI PRICE\n"
        "집계 함수: SUM, COUNT\n"
        "적용 행 수: 12,269\n"
        "결과값: 386,933,825,518"
    )
    final = compute_final_answer(
        "2026년 1월 총 sell-out 금액은?",
        {
            "answer": "",
            "sources": ["document"],
            "tool_calls": [],
            "markdown_response": {"fact_md": ""},
            "file_context": "## 업로드 파일 SQL 결과\n상태: 확인됨\n386933825518",
            "deterministic_file_answer": answer,
        },
        "file-aggregate",
    )

    assert "386,933,825,518" in final.text
    assert "적용 행 수: 12,269" in final.text


def test_file_page_answer_is_not_rewritten_as_market_brand_compare(monkeypatch) -> None:
    answer = (
        "2페이지 기준 2023년 골다공증 51.8 million, 골감소증 139.1 million이며, "
        "2043년에는 각각 63.7 million과 168.2 million입니다."
    )
    monkeypatch.setattr(GenosClient, "stream_answer", lambda *_args, **_kwargs: iter((answer,)))

    final = compute_final_answer(
        "2페이지에서 2023년과 2043년 골다공증·골감소증 환자 수를 각각 알려줘.",
        {
            "answer": "",
            "sources": ["document"],
            "tool_calls": [],
            "markdown_response": {"fact_md": ""},
            "file_context": (
                "검색 범위: 문서 전체 키워드 검색 + 지정 페이지 직접 조회 (2페이지)\n"
                "2023 51.8 million 139.1 million; 2043 63.7 million 168.2 million"
            ),
            "context_scope": "FILE",
        },
        "file-page",
    )

    assert all(value in final.text for value in ("51.8", "139.1", "63.7", "168.2"))
    assert "표에 포함된 확정 데이터만" not in final.text


def test_file_scope_postprocess_market_message_is_rejected(monkeypatch) -> None:
    monkeypatch.setattr(
        GenosClient,
        "stream_answer",
        lambda *_args, **_kwargs: iter(
            ["시장 도구 미호출로 일반뷰 브랜드 비교를 완료할 수 없습니다."]
        ),
    )
    result = {
        "answer": "업로드 파일에서 확인된 근거만 사용해 답변합니다.",
        "context_scope": "FILE",
        "sources": ["document"],
        "tool_calls": [],
        "markdown_response": {"markdown": "", "fact_md": "", "data_md": ""},
    }

    final = compute_final_answer(
        "동아제약과 동화약품 비교",
        result,
        "conversation-1",
    )

    assert "시장 도구" not in final.text
    assert "일반뷰" not in final.text
    assert "업로드 파일" in final.text


def test_file_scope_postprocess_actual_missing_market_tool_message_is_rejected(monkeypatch) -> None:
    monkeypatch.setattr(
        GenosClient,
        "stream_answer",
        lambda *_args, **_kwargs: iter(
            ["필요 도구(시장 지표 조회)가 이번 턴에 실행되지 않았습니다."]
        ),
    )
    result = {
        "answer": "업로드 파일에서 확인된 근거만 사용해 답변합니다.",
        "context_scope": "FILE",
        "sources": ["document"],
        "tool_calls": [],
        "markdown_response": {"markdown": "", "fact_md": "", "data_md": ""},
    }

    final = compute_final_answer(
        "동아제약과 동화약품 비교",
        result,
        "conversation-1",
    )

    assert "시장 지표 조회" not in final.text
    assert "업로드 파일" in final.text


def test_file_page_answer_backfills_requested_numeric_evidence(monkeypatch) -> None:
    monkeypatch.setattr(
        GenosClient,
        "stream_answer",
        lambda *_args, **_kwargs: iter(("지정 페이지의 핵심 내용을 확인했습니다.",)),
    )

    final = compute_final_answer(
        "2페이지에서 2023년과 2043년 환자 수를 각각 알려줘.",
        {
            "answer": "",
            "sources": ["document"],
            "tool_calls": [],
            "markdown_response": {"fact_md": ""},
            "file_context": (
                "검색 범위: 문서 전체 키워드 검색 + 지정 페이지 직접 조회 (2페이지)\n\n"
                "[1] F3.pdf | p.2\n"
                "In 2023 there were 51.8 million and 139.1 million patients. "
                "By 2043 the totals rise to 63.7 million and 168.2 million."
            ),
            "context_scope": "FILE",
        },
        "file-page-numeric",
    )

    assert all(value in final.text for value in ("51.8", "139.1", "63.7", "168.2"))
    assert "지정 페이지 원문 근거" in final.text


def test_file_page_evidence_ignores_source_metadata_numbers() -> None:
    answer = ensure_file_page_evidence(
        "2페이지에서 2023년과 2043년 환자 수를 각각 알려줘.",
        "지정 페이지의 핵심 내용을 확인했습니다.",
        (
            "검색 범위: 문서 전체 키워드 검색 + 지정 페이지 직접 조회 (2페이지)\n\n"
            "[1] F3.pdf (document_id=113292)\n[DA] TEMP_DOCUMENT_1845.pdf | p.2\n\n"
            "[2] F3.pdf (document_id=113292)\n[DA] TEMP_DOCUMENT_1845.pdf | p.2\n\n"
            "In 2023 there were 51.8 million and 139.1 million patients. "
            "By 2043 the totals rise to 63.7 million and 168.2 million."
        ),
    )

    assert all(value in answer for value in ("51.8", "139.1", "63.7", "168.2"))


def test_file_kol_answer_is_not_rewritten_as_unconnected_market_source(monkeypatch) -> None:
    answer = "31페이지 KOL은 anabolic 치료 후 Prolia 같은 antiresorptive 치료가 필요하다고 설명합니다."
    monkeypatch.setattr(GenosClient, "stream_answer", lambda *_args, **_kwargs: iter((answer,)))

    final = compute_final_answer(
        "31페이지 KOL Insights의 anabolic 치료와 Prolia 중단 의견을 요약해줘.",
        {
            "answer": "",
            "sources": ["document"],
            "tool_calls": [],
            "markdown_response": {"fact_md": ""},
            "file_context": (
                "검색 범위: 문서 전체 키워드 검색 + 지정 페이지 직접 조회 (31페이지)\n"
                "KOL Insights: anabolic treatment should be followed by an antiresorptive; "
                "Prolia discontinuation needs transition therapy."
            ),
            "context_scope": "FILE",
        },
        "file-kol",
    )

    assert "anabolic" in final.text
    assert "Prolia" in final.text
    assert "운영 데이터에 미보유" not in final.text


def test_genos_markdown_file_kol_skips_market_source_trap(monkeypatch) -> None:
    monkeypatch.setattr(
        GenosClient,
        "_chat_text",
        lambda *_args, **_kwargs: (
            "31페이지 KOL은 anabolic 치료 후 Prolia 중단 시 전환 치료가 필요하다고 설명합니다."
        ),
    )
    result = {
        "answer": "",
        "sources": ["document"],
        "tool_calls": [],
        "markdown_response": {"fact_md": "", "allowed_numbers": ()},
        "file_context": (
            "검색 범위: 문서 전체 키워드 검색 + 지정 페이지 직접 조회 (31페이지)\n"
            "KOL Insights: anabolic treatment should be followed by an antiresorptive; "
            "Prolia discontinuation needs transition therapy."
        ),
    }

    answer = "".join(
        GenosClient(base_url="http://unused", token="token").stream_answer(
            "31페이지 KOL Insights의 anabolic 치료와 Prolia 중단 의견을 요약해줘.",
            result,
        )
    )

    assert "anabolic" in answer
    assert "Prolia" in answer
    assert "운영 데이터에 미보유" not in answer


def test_answer_question_direct_agent_loop_preserves_unsupported_brand_contract(monkeypatch) -> None:
    class Resolver:
        def resolve(self, _question: str, *, allow_default: bool = False):
            raise UnsupportedBrandError("unsupported")

    class Dependencies:
        router = BQRouter()
        resolver = Resolver()

        def agent_loop_dependencies(self):
            raise AssertionError("unsupported brands should fail closed before ToolUseAgent")

    monkeypatch.setattr(service_app, "build_chat_agent_dependencies", lambda *, external_mode="fixture": Dependencies())

    item = service_app._answer_question(
        SessionStore(),
        _market_scope_resolver(),
        _fake_agent_factory,
        "타이레놀 경쟁 구도 변화",
        "live",
        None,
        use_direct_agent_loop=True,
    )

    result = item["result"]
    assert result["sources"] == ["unsupported_brand"]
    assert result["tool_calls"] == []
    assert result["router_diagnostics"] == service_app.router_diagnostics(Dependencies.router)
    assert "지원하지 않는 브랜드" in result["answer"]


def test_answer_question_direct_agent_loop_allows_portfolio_scope_without_single_brand_resolution(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Resolver:
        def resolve(self, _question: str, *, allow_default: bool = False):
            raise UnsupportedBrandError("portfolio scope is not a single brand")

    class Dependencies:
        router = BQRouter()
        resolver = Resolver()

        def agent_loop_dependencies(self):
            captured["loop_dependencies"] = True
            return "portfolio-loop-deps"

    class Loop:
        def answer(self, question: str) -> dict:
            captured["loop_question"] = question
            return {"answer": "portfolio-loop", "sources": ["cache"], "tool_calls": [{"tool": "portfolio_decline_analysis"}]}

    monkeypatch.setattr(service_app, "build_chat_agent_dependencies", lambda *, external_mode="fixture": Dependencies())
    monkeypatch.setattr(service_app, "build_tool_use_agent", lambda dependencies: Loop())

    item = service_app._answer_question(
        SessionStore(),
        _market_scope_resolver(),
        _fake_agent_factory,
        "JW 주요 브랜드 중 최근 시장점유율이 하락한 게 있으면 어떤 브랜드인지 분석해줘",
        "live",
        None,
        use_direct_agent_loop=True,
    )

    assert item["result"]["answer"] == "portfolio-loop"
    assert item["result"]["tool_calls"] == [{"tool": "portfolio_decline_analysis"}]
    assert captured["loop_dependencies"] is True
    assert captured["loop_question"] == "JW 주요 브랜드 중 최근 시장점유율이 하락한 게 있으면 어떤 브랜드인지 분석해줘"


def test_answer_question_direct_agent_loop_allows_short_portfolio_scope(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Resolver:
        def resolve(self, _question: str, *, allow_default: bool = False):
            raise UnsupportedBrandError("portfolio scope is not a single brand")

    class Dependencies:
        router = BQRouter()
        resolver = Resolver()

        def agent_loop_dependencies(self):
            captured["loop_dependencies"] = True
            return "portfolio-loop-deps"

    class Loop:
        def answer(self, question: str) -> dict:
            captured["loop_question"] = question
            return {"answer": "portfolio-loop", "sources": ["cache"], "tool_calls": [{"tool": "portfolio_decline_analysis"}]}

    monkeypatch.setattr(service_app, "build_chat_agent_dependencies", lambda *, external_mode="fixture": Dependencies())
    monkeypatch.setattr(service_app, "build_tool_use_agent", lambda dependencies: Loop())

    item = service_app._answer_question(
        SessionStore(),
        _market_scope_resolver(),
        _fake_agent_factory,
        "JW 주요 브랜드 중 하락한 거 원인 분석",
        "live",
        None,
        use_direct_agent_loop=True,
    )

    assert item["result"]["answer"] == "portfolio-loop"
    assert item["result"]["tool_calls"] == [{"tool": "portfolio_decline_analysis"}]
    assert captured["loop_dependencies"] is True
    assert captured["loop_question"] == "JW 주요 브랜드 중 하락한 거 원인 분석"


def test_create_app_exposes_chat_routes() -> None:
    app = create_app()

    paths = {route.path for route in app.routes}

    assert "/chat" in paths
    assert "/chat/answer" in paths
    assert "/chat/stream" in paths
    assert "/__version" in paths
    assert "/healthz" in paths
    assert "/readyz" in paths


def test_version_endpoint_reports_runtime_and_policy_provenance(monkeypatch) -> None:
    monkeypatch.setenv("JW_CHAT_RELEASE_ID", "release-test")
    monkeypatch.setenv("JW_CHAT_GIT_SHA", "abc123")
    monkeypatch.setenv("JW_CHAT_IMAGE_DIGEST", "sha256:test")
    monkeypatch.setenv("JW_CHAT_BUILT_AT", "2026-06-29T00:00:00Z")
    monkeypatch.setenv("GENOS_SERVING_ID", "517")
    monkeypatch.setenv("GENOS_FINAL_SERVING_ID", "514")
    monkeypatch.setenv("GENOS_PLANNER_SERVING_ID", "508")
    app = create_app()
    client = TestClient(app)

    response = client.get("/__version")

    assert response.status_code == 200
    payload = response.json()
    assert payload["release_id"] == "release-test"
    assert payload["git_sha"] == "abc123"
    assert payload["image_digest"] == "sha256:test"
    assert payload["model_family"] == "gemini-3-flash-preview"
    assert payload["serving_common_router"] == "517"
    assert payload["serving_final"] == "514"
    assert payload["serving_planner"] == "508"
    assert payload["policy_versions"]["claim_policy_version"].startswith("sha256:")
    assert payload["policy_versions"]["answer_contract_version"].startswith("sha256:")
    assert payload["policy_versions"]["routing_registry_version"].startswith("sha256:")


def test_static_frontend_root_serves_same_origin_index() -> None:
    app = create_app()
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "JW Chat Agent POC" in response.text
    assert 'queryParams.get("api")' in response.text
    assert "window.location.origin" in response.text
    assert "/jw-chat-agent" in response.text
    assert "https://jwai-dev.jwhealthcare.com/jw-chat-agent" not in response.text


def test_static_frontend_index_route_does_not_shadow_api_routes() -> None:
    app = create_app()
    client = TestClient(app)

    index_response = client.get("/index.html")
    health_response = client.get("/healthz")

    assert index_response.status_code == 200
    assert index_response.headers["content-type"].startswith("text/html")
    assert health_response.status_code == 200
    assert health_response.json() == {"status": "ok"}


def test_static_frontend_direct_prefix_routes_serve_index() -> None:
    app = create_app()
    client = TestClient(app)

    for path in ("/jw-chat-agent", "/jw-chat-agent/"):
        response = client.get(path)

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert "JW Chat Agent POC" in response.text


def test_genos_client_returns_cache_metric_answer_without_llm(monkeypatch) -> None:
    def fail_stream(*_args, **_kwargs):
        raise AssertionError("cache-only metric answers must not be sent through GenOS")

    monkeypatch.setattr(GenosClient, "_stream_chat", fail_stream)
    client = GenosClient(token="dummy-token")
    agent_result = {
        "answer": "리바로 MS 3.76%, 순위 6/516, 브랜드 CAGR 12.09%",
        "sources": ["cache"],
        "tool_calls": [{"source": "cache"}],
    }

    chunks = list(client.stream_answer("리바로 시장 점유율은?", agent_result))

    assert len(chunks) > 1
    assert "".join(chunks) == agent_result["answer"]


def test_genos_client_cleans_cache_only_markdown_without_llm(monkeypatch) -> None:
    def fail_stream(*_args, **_kwargs):
        raise AssertionError("cache-only metric answers must not be sent through GenOS")

    monkeypatch.setattr(GenosClient, "_stream_chat", fail_stream)
    client = GenosClient(token="dummy-token")
    agent_result = {
        "answer": (
            "###리바로 매출\n\n"
            "| 기간 | 매출 | 시장 점유율 |\n"
            "| --- |--- | --- |\n"
            "|2026-04|84.93억원 |3.76% |\n\n"
            "2026-04:84.93억원, 87.11억원에서84.93억원으로 변경된 점양해 바랍니다."
        ),
        "sources": ["cache"],
        "tool_calls": [{"source": "cache"}],
    }

    answer = "".join(client.stream_answer("리바로 매출", agent_result))

    assert "### 리바로 매출" in answer
    assert "| --- | --- | --- |" in answer
    assert "| 2026-04 | 84.93억원 | 3.76% |" in answer
    assert "2026-04: 84.93억원" in answer
    assert "에서 84.93억원" in answer
    assert "점 양해" in answer


def test_genos_client_relays_drug_info_without_llm(monkeypatch) -> None:
    def fail_stream(*_args, **_kwargs):
        raise AssertionError("MFDS permission facts must be relayed without final LLM prose")

    monkeypatch.setattr(GenosClient, "_stream_chat", fail_stream)
    client = GenosClient(token="dummy-token")
    fact_md = (
        "### MFDS 허가정보\n\n"
        "| 품목명 | 업체 | 허가일 | 구분 | 성분 | 저장법 | 유효기간 |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        "| 리바로정1밀리그램(피타바스타틴칼슘수화물) | 제이더블유중외제약(주) | 20050106 | 전문의약품 | Pitavastatin Calcium Hydrate | 차광기밀용기 | 제조일로부터 36 개월 |"
    )
    agent_result = {
        "markdown_response": {
            "fact_md": fact_md,
            "sources_md": "## 출처\n\n- 외부: search_drug_info · mfds_permission_detail — item_seq=200500287",
        },
        "tool_calls": [{"tool": "search_drug_info", "source": "external_api"}],
    }

    answer = "".join(client.stream_answer("리바로 식약처 허가정보 알려줘", agent_result))

    assert "리바로정1밀리그램" in answer
    assert "제이더블유중외제약" in answer
    assert "20050106" in answer
    assert "MFDS 허가정보" in answer
    assert "| 품목명 | 업체 | 허가일 | 구분 | 성분 | 저장법 | 유효기간 |" in answer
    assert "## 출처" in answer


def test_genos_client_appends_policy_notice_after_llm_answer(monkeypatch) -> None:
    def stream_chat(_self, _messages):
        yield "본문 답변입니다."

    monkeypatch.setattr(GenosClient, "_stream_chat", stream_chat)
    client = GenosClient(token="dummy-token")
    agent_result = {
        "answer": "fallback",
        "sources": ["external_api"],
        "tool_calls": [
            {
                "tool": "matching_policy_notice",
                "source": "external_api",
                "summary_text": "해외 FDA/OpenFDA/Orange Book은 pitavastatin 성분 기준 자료입니다.",
            },
            {
                "tool": "mfds_fda_orangebook",
                "source": "external_api",
                "summary_text": "mfds_fda_orangebook returned HTTP 503",
            },
        ],
    }

    answer = "".join(client.stream_answer("리바로 특허", agent_result))

    assert answer == (
        "본문 답변입니다.\n\n주의:\n"
        "- 해외 FDA/OpenFDA/Orange Book은 pitavastatin 성분 기준 자료입니다."
    )
    assert "Book 자료는" not in answer


def test_genos_prompt_keeps_policy_notice_text_out_of_llm_prompt() -> None:
    agent_result = {
        "answer": "fallback",
        "sources": ["external_api"],
        "tool_calls": [
            {
                "tool": "matching_policy_notice",
                "source": "external_api",
                "summary_text": "해외 CT/OpenFDA 결과는 pitavastatin 성분 기준 동향입니다.",
            },
            {
                "tool": "clinicaltrials_v2_search",
                "source": "external_api",
                "summary_text": "clinicaltrials_v2_search returned HTTP 200",
            },
        ],
    }

    prompt = GenosClient._prompt("리바로 임상", agent_result)

    assert '"notice_count": 1' in prompt
    assert "해외 CT/OpenFDA 결과는 pitavastatin 성분 기준 동향입니다." not in prompt


def test_sse_delta_frames_multiline_notice_as_data_lines() -> None:
    event = _sse_delta("\n\n주의:\n- Orange Book 문구")

    assert event == "event: delta\ndata: \ndata: \ndata: 주의:\ndata: - Orange Book 문구\n\n"


def test_chunked_sse_preserves_period_value_separators() -> None:
    answer = (
        "아토젯 월별 MS: 2025-11 5.02% → 2025-12 5.14% → "
        "2026-01 5.06% → 2026-02 5.09% → 2026-03 5.22%"
    )

    sse = "".join(_sse_delta(token) for token in chunk_text(answer))
    reconstructed = "".join(
        "\n".join(line.removeprefix("data: ") for line in block.splitlines()[1:])
        for block in sse.split("\n\n")
        if block.startswith("event: delta\n")
    )

    assert "2025-12 5.14%" in reconstructed
    assert "2026-03 5.22%" in reconstructed
    assert "2025-125.14%" not in reconstructed


def test_markdown_timeout_fallback_keeps_causal_structure_and_deterministic_sources(monkeypatch) -> None:
    def timeout_chat(_self: GenosClient, _messages: list[dict[str, str]]) -> str:
        raise requests.Timeout("simulated final generation timeout")

    monkeypatch.setattr(GenosClient, "_chat_text", timeout_chat)
    response = MarkdownResponseBuilder().build(
        brand="리바로",
        calls=[
            {
                "tool": "get_brand_metric",
                "source": "cache",
                "render_data": {
                    "brand": "리바로",
                    "metric": "sales",
                    "period": "2026-04",
                    "sales_억원": 84.93,
                    "ms_recent_pct": 3.76,
                    "rank": 6,
                    "total_brands_in_market": 516,
                },
            }
        ],
        sources=["cache"],
    )

    answer = "".join(
        GenosClient(token="dummy-token").stream_answer("리바로 매출", {"markdown_response": response.to_dict()})
    )

    assert "## 인과 분석" not in answer
    assert "시장 내 침투가 강화되는지 또는 방어 압력이 커지는지" in answer
    assert "출처: UBIST" not in answer
    assert answer.rfind("## 출처") > answer.rfind("시장 내 침투가 강화되는지")
    assert answer.strip().endswith("| UBIST | 2026-04 | — | — | 516 | 전체 | 억원 |")


def test_stream_endpoint_does_not_emit_charts_for_single_metric(monkeypatch) -> None:
    def stream_answer(_self, _question, _result):
        yield "리바로 점유율은 3.76%, 순위는 6위입니다."

    monkeypatch.setattr(GenosClient, "stream_answer", stream_answer)
    app = create_app()
    client = TestClient(app)

    response = client.get("/chat/stream", params={"question": "리바로 점유율이랑 순위 알려줘"})

    assert response.status_code == 200
    assert "event: delta" in response.text
    assert "event: charts" not in response.text
    assert "event: done" in response.text


def test_stream_endpoint_emits_trace_metadata_without_changing_answer(monkeypatch) -> None:
    class TraceAgent:
        def __init__(self, *, external_mode: str = "live") -> None:
            self.external_mode = external_mode

        def answer(self, _question: str, _documents=None) -> dict:
            return {
                "answer": "fallback",
                "sources": ["cache"],
                "router_diagnostics": {"mode": "agent_loop"},
                "decomposition": [{"intent": "agent_loop", "status": "ok"}],
                "tool_calls": [
                    {
                        "tool": "get_brand_metric",
                        "source": "cache",
                        "render_data": {
                            "brand": "리바로",
                            "metric": "market_share",
                            "period": "2026-04",
                            "sales_억원": 84.93,
                            "ms_recent_pct": 3.76,
                            "rank": 6,
                            "total_brands_in_market": 470,
                        },
                    }
                ],
                "markdown_response": {
                    "fact_md": (
                        "## 확정 fact set\n\n"
                        "### 필수 답변 fact\n\n"
                        "| 항목 | 값 |\n"
                        "| --- | --- |\n"
                        "| 브랜드 핵심 지표 | 리바로 2026-04 매출 84.93억원 시장점유율 3.76% 순위 6/470 |"
                    ),
                    "sources_md": "## 출처\n\n- 데이터: UBIST (2026-04)",
                },
            }

    def stream_answer(_self, _question, _result):
        yield "리바로는 2026-04 기준 매출 84.93억원, 시장점유율 3.76%, 순위 6/470위입니다."

    monkeypatch.setattr(GenosClient, "stream_answer", stream_answer)
    app = create_app(agent_factory=lambda external_mode="live": TraceAgent(external_mode=external_mode))
    client = TestClient(app)

    response = client.get("/chat/stream", params={"question": "리바로 순위 알려줘"})

    assert response.status_code == 200
    assert "리바로는 2026-04 기준 매출 84.93억원" in response.text
    trace_blocks = [block for block in response.text.split("\n\n") if block.startswith("event: trace\n")]
    assert len(trace_blocks) == 1
    trace = json.loads(trace_blocks[0].split("data: ", 1)[1])
    assert trace["route"]["mode"] == "agent_loop"
    assert trace["tools_called"] == ["get_brand_metric"]
    assert trace["answer_contract_status"]["intent"] == "ranking"
    assert trace["answer_contract_status"]["status"] == "pass"
    assert trace["token_usage"]["available"] is False


def test_trace_envelope_reports_query_spec_provenance_for_query_layer_calls() -> None:
    result = {
        "router_diagnostics": {"mode": "agent_loop", "deterministic_execution": True},
        "tool_calls": [
            {
                "tool": "get_brand_metric",
                "render_data": {
                    "metric": "query_spec",
                    "query_spec": {"market": "ml_006", "group_by": ["channel"]},
                },
            }
        ],
        "markdown_response": {"fact_md": "query(spec) fact"},
    }

    trace = trace_envelope(
        question="리바로 채널과 세그먼트 기준 포지셔닝",
        result=result,
        answer="채널별 표",
        charts=[],
        timing={"stages": []},
        conversation_id=None,
    )

    assert trace["tools_called"] == ["query_spec"]


def test_stream_endpoint_emits_series_chart_from_verified_facts(monkeypatch) -> None:
    class SeriesAgent:
        def __init__(self, *, external_mode: str = "live") -> None:
            self.external_mode = external_mode

        def answer(self, _question: str, _documents=None) -> dict:
            return {
                "answer": "리바로 최근 매출 추이를 확인했습니다.",
                "sources": ["cache"],
                "tool_calls": [
                    {
                        "source": "cache",
                        "tool": "get_brand_metric",
                        "render_data": {
                            "brand": "리바로",
                            "metric": "series",
                            "source_label": "UBIST",
                            "brand_value_series_10pt": [
                                {"period": "2026-03", "value_krw": 8_711_248_139.54},
                                {"period": "2026-04", "value_krw": 8_493_234_217.11},
                            ],
                        },
                    }
                ],
            }

    def stream_answer(_self, _question, _result):
        yield "리바로 최근 매출 추이입니다."

    monkeypatch.setattr(GenosClient, "stream_answer", stream_answer)
    app = create_app(agent_factory=lambda external_mode="live": SeriesAgent(external_mode=external_mode))
    client = TestClient(app)

    response = client.get("/chat/stream", params={"question": "리바로 최근 매출 추이"})

    assert response.status_code == 200
    assert "event: charts" in response.text
    assert '"title":"리바로 매출 추이"' in response.text
    assert "8711248139.54" in response.text
    assert "event: done" in response.text


def test_chat_answer_returns_same_final_markdown_and_metadata_as_stream(monkeypatch) -> None:
    class SeriesAgent:
        def __init__(self, *, external_mode: str = "live") -> None:
            self.external_mode = external_mode

        def answer(self, _question: str, _documents=None) -> dict:
            return {
                "answer": "리바로 최근 매출 추이를 확인했습니다.",
                "sources": ["cache"],
                "tool_calls": [
                    {
                        "source": "cache",
                        "tool": "get_brand_metric",
                        "render_data": {
                            "brand": "리바로",
                            "metric": "series",
                            "source_label": "UBIST",
                            "brand_value_series_10pt": [
                                {"period": "2026-03", "value_krw": 8_711_248_139.54},
                                {"period": "2026-04", "value_krw": 8_493_234_217.11},
                            ],
                        },
                    }
                ],
            }

    def stream_answer(_self, _question, _result):
        yield "리바로 최근 매출 추이입니다.\n\n| 기간 | 매출 |\n| --- | --- |\n| 2026-03 | 87.11억원 |"

    monkeypatch.setattr(GenosClient, "stream_answer", stream_answer)
    app = create_app(agent_factory=lambda external_mode="live": SeriesAgent(external_mode=external_mode))
    client = TestClient(app)
    payload = {"question": "리바로 최근 매출 추이"}

    answer_response = client.post("/chat/answer", json=payload)
    stream_response = client.get("/chat/stream", params=payload)

    assert answer_response.status_code == 200
    assert stream_response.status_code == 200
    body = answer_response.json()
    assert _normalize_markdown_spacing(body["text"]) == _normalize_markdown_spacing(
        _reconstruct_answer_from_sse(stream_response.text)
    )
    assert body["sources"] == ["cache"]
    assert body["conversation_id"]
    assert body["charts"][0]["title"] == "리바로 매출 추이"
    assert body["trace"]["answer_contract_status"]["status"]


def test_stream_endpoint_emits_timing_metadata_only(monkeypatch) -> None:
    class TimedAgent:
        def __init__(self, *, external_mode: str = "live") -> None:
            self.external_mode = external_mode

        def answer(self, _question: str, _documents=None) -> dict:
            return {
                "answer": "리바로 답변입니다.",
                "sources": ["UBIST"],
                "tool_calls": [],
                "timing": {
                    "started_at_monotonic": 1.0,
                    "stages": [{"name": "query", "elapsed_ms": 12.34, "detail": "get_metric"}],
                },
            }

    def stream_answer(_self, _question, _result):
        yield "리바로 답변입니다."

    monkeypatch.setattr(GenosClient, "stream_answer", stream_answer)
    app = create_app(agent_factory=lambda external_mode="live": TimedAgent(external_mode=external_mode))
    client = TestClient(app)

    response = client.get("/chat/stream", params={"question": "리바로 매출"})

    assert response.status_code == 200
    assert "event: timing" in response.text
    assert '"query"' in response.text
    answer_events = "\n\n".join(
        block
        for block in response.text.split("\n\n")
        if block.startswith("event: delta\n") or block.startswith("event: markdown_block\n")
    )
    assert "## 처리 시간" not in answer_events
    assert "event: done" in response.text


def test_stream_endpoint_applies_channel_claim_policy_without_markdown_response(monkeypatch) -> None:
    class ChannelAgent:
        def __init__(self, *, external_mode: str = "live") -> None:
            self.external_mode = external_mode

        def answer(self, _question: str, _documents=None) -> dict:
            return {
                "answer": "channel fallback",
                "sources": ["UBIST"],
                "tool_calls": [],
            }

    def stream_answer(_self, _question, _result):
        yield (
            "| 채널 | 시장점유율 | 매출 || --- | --- | --- || 의원 | 3.37% | 41.93억원 || "
            "종합병원 | 4.22% | 20.60억원 || 상급종합병원 | 4.49% | 17.56억원 |"
        )

    monkeypatch.setattr(GenosClient, "stream_answer", stream_answer)
    app = create_app(agent_factory=lambda external_mode="live": ChannelAgent(external_mode=external_mode))
    client = TestClient(app)

    response = client.get("/chat/stream", params={"question": "리바로 채널별 매출"})

    assert response.status_code == 200
    assert "현재 데이터만으로 확인할 수 없" in response.text
    assert "의원 41.93억원" in response.text
    assert "event: done" in response.text


def test_markdown_table_blocks_are_sent_as_atomic_sse_events() -> None:
    answer = "채널 표입니다.\n\n| 채널 | 매출 |\n| --- | --- |\n| 의원 | 41.93억원 |\n\n요약입니다."

    encoded = "".join(iter_markdown_sse_events(answer))

    assert "event: markdown_block" in encoded
    assert '"markdown":"\\n\\n| 채널 | 매출 |\\n| --- | --- |\\n| 의원 | 41.93억원 |\\n\\n"' in encoded
    assert "event: delta\ndata: | 채널" not in encoded


def test_stream_endpoint_keeps_timing_out_of_answer_body(monkeypatch) -> None:
    class TimedAgent:
        def __init__(self, *, external_mode: str = "live") -> None:
            self.external_mode = external_mode

        def answer(self, _question: str, _documents=None) -> dict:
            return {
                "answer": "리바로 답변입니다.",
                "sources": ["UBIST"],
                "tool_calls": [],
                "timing": {
                    "started_at_monotonic": 1.0,
                    "stages": [{"name": "query", "elapsed_ms": 12.34, "detail": "get_metric"}],
                },
            }

    def stream_answer(_self, _question, _result):
        yield "| 항목 | 값 |\n| --- | --- |\n| 매출 | 1억원 |"

    monkeypatch.setattr(GenosClient, "stream_answer", stream_answer)
    app = create_app(agent_factory=lambda external_mode="live": TimedAgent(external_mode=external_mode))
    client = TestClient(app)

    response = client.get("/chat/stream", params={"question": "리바로 매출"})

    assert response.status_code == 200
    answer_events = "\n\n".join(
        block
        for block in response.text.split("\n\n")
        if block.startswith("event: delta\n") or block.startswith("event: markdown_block\n")
    )
    assert "## 처리 시간" not in answer_events
    assert "event: timing" in response.text
    assert response.text.count("event: timing") == 1


def test_stream_endpoint_emits_user_friendly_source_labels(monkeypatch) -> None:
    def stream_answer(_self, _question, _result):
        yield "본문"

    monkeypatch.setattr(GenosClient, "stream_answer", stream_answer)
    app = create_app(agent_factory=_fake_agent_factory)
    client = TestClient(app)

    response = client.get("/chat/stream", params={"question": "리바로 매출"})

    assert response.status_code == 200
    assert "event: sources\ndata: UBIST\n\n" in response.text
    assert "event: sources\ndata: cache\n\n" not in response.text


def test_stream_endpoint_falls_back_to_fact_text_without_error_event(monkeypatch) -> None:
    response = MarkdownResponseBuilder().build(
        brand="리바로",
        calls=[
            {
                "tool": "agent_calculation",
                "source": "cache",
                "render_data": {
                    "brand": "리바로",
                    "metric": "market_share_delta",
                    "period": "2026-03→2026-04",
                    "from_ms_pct": 3.46,
                    "to_ms_pct": 3.33,
                    "ms_delta_pct": -0.13,
                },
            }
        ],
        sources=["cache"],
    )

    class AgentWithMarkdown:
        def __init__(self, *, external_mode: str = "live") -> None:
            self.external_mode = external_mode

        def answer(self, _question: str, _documents=None) -> dict:
            return {
                "answer": "old fallback",
                "sources": ["cache"],
                "tool_calls": [],
                "markdown_response": response.to_dict(),
            }

    def stream_answer(_self: GenosClient, _question: str, _result: dict):
        yield "MS:2025-11 5.02% → 2025-125.14% → 2026-035.22%"
        raise requests.Timeout("Flash generation timed out")

    monkeypatch.setattr(GenosClient, "stream_answer", stream_answer)
    app = create_app(agent_factory=lambda external_mode="live": AgentWithMarkdown(external_mode=external_mode))
    client = TestClient(app)

    sse = client.get("/chat/stream", params={"question": "리바로 3달전 대비 점유율 변화"}).text

    assert "event: error" not in sse
    assert "event: delta" in sse
    assert "점유율 변화" in sse
    reconstructed = "".join(
        "\n".join(line.removeprefix("data: ") for line in block.splitlines()[1:])
        for block in sse.split("\n\n")
        if block.startswith("event: delta\n")
    )
    assert "2026-03" in reconstructed
    assert "2026-04" in reconstructed
    assert "event: done" in sse


def test_stream_endpoint_cleans_fact_fallback_when_stream_fails(monkeypatch) -> None:
    response = MarkdownResponseBuilder().build(
        brand="리바로",
        calls=[
            {
                "tool": "get_brand_metric",
                "source": "cache",
                "render_data": {
                    "brand": "리바로",
                    "metric": "series",
                    "level_top5_trend_series": [
                        {
                            "brand": "아토젯",
                            "rank": 4,
                            "ms_recent_pct": 5.16,
                            "share_delta_pctp": 0.14,
                            "value_recent": 11_649_000_000,
                            "value_delta_krw": 471_000_000,
                            "series": [
                                {"period": "2025-11", "ms_pct": 5.02, "value_억원": 110.0, "rank": 4},
                                {"period": "2025-12", "ms_pct": 5.14, "value_억원": 112.0, "rank": 4},
                                {"period": "2026-03", "ms_pct": 5.22, "value_억원": 117.0, "rank": 4},
                            ],
                        }
                    ],
                },
            }
        ],
        sources=["cache"],
    )

    class AgentWithTrendMarkdown:
        def __init__(self, *, external_mode: str = "live") -> None:
            self.external_mode = external_mode

        def answer(self, _question: str, _documents=None) -> dict:
            return {
                "answer": "old fallback",
                "sources": ["cache"],
                "tool_calls": [],
                "markdown_response": response.to_dict(),
            }

    def stream_answer(_self: GenosClient, _question: str, _result: dict):
        raise requests.Timeout("Flash generation timed out")
        yield ""

    monkeypatch.setattr(GenosClient, "stream_answer", stream_answer)
    app = create_app(agent_factory=lambda external_mode="live": AgentWithTrendMarkdown(external_mode=external_mode))
    client = TestClient(app)

    sse = client.get("/chat/stream", params={"question": "리바로 경쟁 구도"}).text
    reconstructed = "".join(
        "\n".join(line.removeprefix("data: ") for line in block.splitlines()[1:])
        for block in sse.split("\n\n")
        if block.startswith("event: delta\n")
    )

    assert "MS: 2025-11 5.02%" in reconstructed
    assert "2025-12 5.14%" in reconstructed
    assert "2026-03 5.22%" in reconstructed
    assert "2025-125.14%" not in reconstructed
    assert "2026-035.22%" not in reconstructed
    assert "event: error" not in sse


def test_stream_endpoint_handles_same_market_default_before_agent_fallback() -> None:
    FakeAgent.calls = []
    app = create_app(agent_factory=_fake_agent_factory, market_scope_resolver=_market_scope_resolver())
    client = TestClient(app)

    response = client.get(
        "/chat/stream",
        params={"conversation_id": "conv-market", "question": "리바로랑 같은 시장 매출"},
    )

    assert response.status_code == 200
    assert "event: conversation\ndata: conv-market\n\n" in response.text
    assert "전략뷰 기준" in response.text
    assert "competitive_dynamics" not in response.text
    assert "| 전략뷰 |" in response.text
    assert "## 주의" not in response.text
    assert "2,256.77억원" in response.text
    assert "84.93억원" not in response.text
    assert FakeAgent.calls == []


def test_stream_endpoint_routes_complex_market_vs_brand_question_to_agent_loop() -> None:
    FakeAgent.calls = []
    app = create_app(agent_factory=_fake_agent_factory, market_scope_resolver=_market_scope_resolver())
    client = TestClient(app)

    response = client.get(
        "/chat/stream",
        params={"conversation_id": "conv-market-vs-brand", "question": "리바로 2월 매출이 떨어진 게 시장 전체 영향이야, 리바로만의 문제야?"},
    )

    assert response.status_code == 200
    assert "fallback:리바로 2월" in response.text
    assert "요청한 매출 필터 중 현재 데이터가 지원하지 않는 조건" not in response.text
    assert FakeAgent.calls == [("리바로 2월 매출이 떨어진 게 시장 전체 영향이야, 리바로만의 문제야?", "live")]


def test_stream_endpoint_answers_strong_view_question_instead_of_deferring() -> None:
    FakeAgent.calls = []
    app = create_app(agent_factory=_fake_agent_factory, market_scope_resolver=_market_scope_resolver())
    client = TestClient(app)

    response = client.get(
        "/chat/stream",
        params={"conversation_id": "conv-view-question", "question": "리바로랑 같은 시장 매출은 어느 기준으로 봐야 해?"},
    )

    assert response.status_code == 200
    assert "어느 기준으로 볼까요" not in response.text
    assert "전략뷰 기준" in response.text
    assert "competitive_dynamics" not in response.text
    assert "| 전략뷰 |" in response.text
    assert "## 주의" not in response.text
    assert "2,256.77억원" in response.text
    assert FakeAgent.calls == []


def test_stream_endpoint_resolves_pending_market_view_reply_deterministically() -> None:
    store = SessionStore()
    store.conversations.set_pending(
        "conv-clarify",
        PendingClarification(
            kind="market_view",
            original_question="리바로랑 같은 시장 매출",
            brand="리바로",
            metric="sales",
            created_at=1.0,
            expires_at=store.conversations.pending_expiry(),
        ),
    )
    app = create_app(agent_factory=_fake_agent_factory, market_scope_resolver=_market_scope_resolver(), store=store)
    client = TestClient(app)

    second = client.get(
        "/chat/stream",
        params={"conversation_id": "conv-clarify", "question": "전략뷰"},
    )

    assert second.status_code == 200
    assert "전략뷰 기준" in second.text
    assert "2,256.77억원" in second.text


def test_stream_endpoint_keeps_pending_isolated_by_conversation_id() -> None:
    FakeAgent.calls = []
    store = SessionStore()
    store.conversations.set_pending(
        "conv-a",
        PendingClarification(
            kind="market_view",
            original_question="리바로랑 같은 시장 매출",
            brand="리바로",
            metric="sales",
            created_at=1.0,
            expires_at=store.conversations.pending_expiry(),
        ),
    )
    app = create_app(agent_factory=_fake_agent_factory, market_scope_resolver=_market_scope_resolver(), store=store)
    client = TestClient(app)

    response = client.get(
        "/chat/stream",
        params={"conversation_id": "conv-b", "question": "전략뷰"},
    )

    assert response.status_code == 200
    assert "fallback:전략뷰" in response.text
    assert FakeAgent.calls == [("전략뷰", "live")]


def test_stream_endpoint_preserves_single_turn_fallback_without_pending() -> None:
    FakeAgent.calls = []
    app = create_app(agent_factory=_fake_agent_factory, market_scope_resolver=_market_scope_resolver())
    client = TestClient(app)

    response = client.get("/chat/stream", params={"conversation_id": "conv-sales", "question": "리바로 매출"})

    assert response.status_code == 200
    assert "fallback:리바로 매출" in response.text
    assert FakeAgent.calls == [("리바로 매출", "live")]


def test_answer_question_reuses_previous_ranked_brand_series_for_anaphora() -> None:
    calls: list[str] = []

    class ContextAgent:
        def answer(self, question: str, _documents=None) -> dict:
            calls.append(question)
            return {
                "answer": "상위 브랜드",
                "resolution": {"canonical_brand": "리바로"},
                "sources": ["UBIST"],
                "tool_calls": [
                    {
                        "tool": "get_brand_metric",
                        "source": "UBIST",
                        "render_data": {
                            "brand": "리바로",
                            "market_id": "ml_006",
                            "period": "2026-04",
                            "level_top5_trend_series": [
                                {
                                    "brand": "로수젯",
                                    "rank": 1,
                                    "series": [
                                        {"period": "2026-03", "value_krw": 19_500_000_000.0, "ms_pct": 8.7, "rank": 1},
                                        {"period": "2026-04", "value_krw": 20_685_385_934.33, "ms_pct": 9.1659, "rank": 1},
                                    ],
                                }
                            ],
                        },
                    }
                ],
            }

    agent = ContextAgent()
    store = SessionStore()
    factory = lambda **_kwargs: agent

    first = service_app._answer_question(store, _market_scope_resolver(), factory, "리바로 시장 상위 3개 브랜드 점유율", "live", "conv-context")
    second = service_app._answer_question(store, _market_scope_resolver(), factory, "그중 1위 브랜드 점유율 추이는?", "live", "conv-context")

    assert first["result"]["tool_calls"]
    assert calls == ["리바로 시장 상위 3개 브랜드 점유율"]
    assert second["result"]["context_fact_reused"] is True
    assert second["result"]["tool_calls"][0]["render_data"]["brand"] == "로수젯"


def test_answer_question_routes_market_concentration_anaphora_to_direct_agent_loop(monkeypatch) -> None:
    store = SessionStore()
    store.conversations.record_exchange(
        "conv-concentration",
        "리바로와 로수젯을 비교해줘",
        "두 브랜드를 비교했습니다.",
        slots=ConversationSlots(anchor_brand="리바로", market="ml_006", market_definition="Statin 시장"),
    )
    captured: list[str] = []

    def direct_loop(question: str, _external_mode: str) -> dict:
        captured.append(question)
        return {
            "answer": "HHI 253.6207",
            "sources": ["UBIST"],
            "tool_calls": [
                {
                    "tool": "get_market_landscape",
                    "source": "UBIST",
                    "render_data": {
                        "anchor_brand": "리바로",
                        "market_id": "ml_006",
                        "period": "2026-05",
                        "hhi_recent": 253.6207,
                    },
                }
            ],
        }

    def fail_factory(*, external_mode: str = "live"):
        raise AssertionError(f"legacy agent must not handle concentration: {external_mode}")

    monkeypatch.setattr(service_app, "_answer_direct_agent_loop", direct_loop)

    item = service_app._answer_question(
        store,
        _market_scope_resolver(),
        fail_factory,
        "이 시장 집중도는 어때?",
        "live",
        "conv-concentration",
        use_direct_agent_loop=True,
    )

    assert captured == ["리바로 시장 집중도는 어때?"]
    assert item["result"]["tool_calls"][0]["render_data"]["hhi_recent"] == 253.6207


def test_answer_question_does_not_guess_unbound_anaphora() -> None:
    FakeAgent.calls = []
    item = service_app._answer_question(
        SessionStore(),
        _market_scope_resolver(),
        _fake_agent_factory,
        "그 브랜드 점유율 추이는?",
        "live",
        "conv-empty-context",
    )

    assert item["result"]["conversation_reference_unresolved"] is True
    assert "어느 브랜드" in item["result"]["answer"]
    assert FakeAgent.calls == []


def test_tool_use_permission_date_survives_final_synthesis(monkeypatch) -> None:
    fact_md = (
        "- 리바로정1밀리그램(피타바스타틴칼슘수화물) (20050106): 허가 품목 = "
        "리바로정1밀리그램(피타바스타틴칼슘수화물) · 허가일 20050106 · "
        "제이더블유중외제약(주) · 성분 Pitavastatin Calcium Hydrate "
        "[식약처 의약품 정보]"
    )

    monkeypatch.setattr(
        GenosClient,
        "_chat_text",
        lambda _self, _messages: (
            "**리바로 허가 정보**\n\n"
            "* **품목명:** 리바로정1밀리그램(피타바스타틴칼슘수화물)\n"
            "* **업체명:** 제이더블유중외제약(주)\n"
            "* **성분:** Pitavastatin Calcium Hydrate"
        ),
    )
    client = GenosClient(token="dummy-token")
    agent_result = {
        "answer": fact_md,
        "router_diagnostics": {"mode": "tool_use_agent", "fallback_code": None},
        "markdown_response": {"fact_md": fact_md, "allowed_numbers": ["20050106"]},
        "tool_calls": [{"tool": "mfds_permission_search", "source": "nedrug_mcp"}],
    }

    answer = "".join(client.stream_answer("리바로 허가일", agent_result))

    assert "허가일은 20050106" in answer
    assert "리바로정1밀리그램" in answer
    assert "제이더블유중외제약" in answer
