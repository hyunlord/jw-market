from __future__ import annotations

import json
from pathlib import Path
import requests
from types import SimpleNamespace

from fastapi.testclient import TestClient

from jw_chat_agent_poc.orchestrator.markdown_response import MarkdownResponseBuilder
from jw_chat_agent_poc.service.answer_safety import chunk_text
from jw_chat_agent_poc.service import app as service_app
from jw_chat_agent_poc.service.genos_client import GenosClient
from jw_chat_agent_poc.service.app import SessionStore, _sse_delta, create_app
from jw_chat_agent_poc.service.conversation import PendingClarification
from jw_chat_agent_poc.service.sse_protocol import iter_markdown_sse_events
from jw_chat_agent_poc.tools.metrics.cache_live import StaticCausePayloadReader, StaticMetricsCacheReader
from jw_chat_agent_poc.tools.metrics.market_scope import MarketScopeResolver
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


def test_answer_question_keeps_document_questions_on_chat_agent_facade(monkeypatch) -> None:
    def fail_direct_dependencies(*, external_mode: str = "fixture"):
        raise AssertionError("document questions must keep the ChatAgent/RAG facade")

    monkeypatch.setattr(service_app, "build_chat_agent_dependencies", fail_direct_dependencies)
    FakeAgent.calls = []

    item = service_app._answer_question(
        SessionStore(),
        _market_scope_resolver(),
        _fake_agent_factory,
        "리바로 경쟁 구도 변화",
        "live",
        None,
        documents=[Path("/tmp/example.pdf")],
        use_direct_agent_loop=True,
    )

    assert item["result"]["answer"] == "fallback:리바로 경쟁 구도 변화"
    assert FakeAgent.calls == [("리바로 경쟁 구도 변화", "live")]


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


def test_create_app_exposes_chat_routes() -> None:
    app = create_app()

    paths = {route.path for route in app.routes}

    assert "/chat" in paths
    assert "/chat/stream" in paths
    assert "/__version" in paths
    assert "/healthz" in paths


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
    assert answer.strip().endswith("- 데이터: UBIST (2026-04)")


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
    assert "market_landscape" not in response.text
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
    assert "market_landscape" not in response.text
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
