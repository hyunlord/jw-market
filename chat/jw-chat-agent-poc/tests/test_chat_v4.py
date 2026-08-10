from __future__ import annotations

import inspect
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import requests
from fastapi.testclient import TestClient
from pydantic import ValidationError

from jw_chat_agent_poc.service import app as service_app
from jw_chat_agent_poc.service.v4.contracts import (
    SOURCE_NAMES,
    Citation,
    PlannerOutput,
    SourceResult,
    ToolQueries,
    V4Answer,
)
from jw_chat_agent_poc.service.v4.executor import ParallelSourceExecutor
from jw_chat_agent_poc.service.v4.gates import apply_v4_gates
from jw_chat_agent_poc.service.v4.llm import planner_client, synthesizer_client
from jw_chat_agent_poc.service.v4.planner import V4Planner
from jw_chat_agent_poc.service.v4.runtime import V4Runtime
from jw_chat_agent_poc.service.v4 import adapters as v4_adapters
from jw_chat_agent_poc.service.v4 import llm as v4_llm
from jw_chat_agent_poc.service.v4.synthesizer import V4Synthesizer, _evidence_fallback


def _plan(**queries: tuple[str, ...]) -> PlannerOutput:
    values = {name: (f"{name} query",) for name in SOURCE_NAMES}
    values.update(queries)
    return PlannerOutput(
        resolved_question="리바로 요즘 어때",
        expanded_intents=("시장", "허가", "임상"),
        tool_queries=ToolQueries(**values),
        linking_plan="first hop is sufficient",
        needs_second_hop=False,
    )


def test_planner_output_requires_all_seven_nonempty_query_lists() -> None:
    payload = {
        "resolved_question": "질문",
        "expanded_intents": ["시장"],
        "tool_queries": {name: [name] for name in SOURCE_NAMES if name != "patent"},
        "linking_plan": "none",
        "needs_second_hop": False,
    }

    with pytest.raises(ValidationError):
        PlannerOutput.model_validate(payload)


def test_mart_adapter_does_not_reenter_legacy_agent_loop() -> None:
    source = inspect.getsource(v4_adapters)

    assert "_answer_direct_agent_loop" not in source
    assert "general_view.answer" in source
    assert "layer.brand_metric" in source


def test_v4_adapter_extracts_identifiers_and_source_specific_queries() -> None:
    assert v4_adapters._nct_id("NCT05151731 선정제외기준 clinical trials") == "NCT05151731"
    assert v4_adapters._hira_code("D69.3 상병 환자수 최근 5년") == "D693"
    assert v4_adapters._ingredient_query("스타틴 계열 최근 안전성 이슈") == "Pitavastatin"
    assert v4_adapters._clinical_query("당뇨망막병증 치료제 최근 임상 동향") == (
        "diabetic retinopathy",
        "condition",
    )


def test_v4_mart_relevance_rejects_external_only_questions() -> None:
    assert v4_adapters._mart_relevant("리바로 요즘 어때") is True
    assert v4_adapters._mart_relevant("리바로 매출 알려줘") is True
    assert v4_adapters._mart_relevant("리바로 효능효과") is False
    assert v4_adapters._mart_relevant("리바로 특허 언제 만료돼") is False


def test_v4_mart_adapter_always_returns_source_result(monkeypatch) -> None:
    from jw_chat_agent_poc.agent_loop import factory
    from jw_chat_agent_poc.service import general_view_routing

    class Resolver:
        def resolve(self, _query, *, allow_default):
            assert allow_default is False
            return SimpleNamespace(canonical_brand="리바로", molecule_en=("Pitavastatin",))

    class QueryLayer:
        def brand_metric(self, brand, metric, period):
            return {"source": "UBIST", "brand": brand, "metric": metric, "period": period}

        def top_brands(self, brand, *, limit, metric):
            return {"source": "UBIST", "brand": brand, "limit": limit, "metric": metric}

    class GeneralView:
        def route(self, _query):
            return general_view_routing.GeneralRoute.EXISTING

    dependencies = SimpleNamespace(
        external=SimpleNamespace(),
        resolver=Resolver(),
        query_layer=QueryLayer(),
    )
    monkeypatch.setattr(factory, "build_chat_agent_dependencies", lambda **_kwargs: dependencies)
    monkeypatch.setattr(
        general_view_routing.GeneralViewService,
        "from_env",
        lambda _resolver: GeneralView(),
    )

    result = v4_adapters.build_source_adapters()["mart"]("리바로 매출 알려줘")

    assert isinstance(result, SourceResult)
    assert result.status == "ok"
    assert result.payload["calls"][0]["metric"] == "sales"


def test_v4_fallback_uses_verified_summaries_instead_of_raw_json() -> None:
    results = (
        SourceResult(
            source="clinicaltrials",
            query="NCT05151731",
            status="ok",
            payload={
                "calls": [
                    {
                        "summary_text": "NCT05151731은 2상 무작위배정 이중눈가림 시험입니다.",
                        "render_data": {"secret_internal": "must-not-be-dumped"},
                    }
                ]
            },
        ),
    )

    answer = _evidence_fallback(results)

    assert "2상 무작위배정 이중눈가림" in answer
    assert "secret_internal" not in answer


def test_v4_synthesizer_sends_detail_rows_in_question_first_layout() -> None:
    class Client:
        def __init__(self) -> None:
            self.messages = None

        def complete(self, messages, *, budget_s=None, max_tokens=None) -> str:
            self.messages = messages
            assert budget_s == 15.0
            assert max_tokens == 1800
            return "2024년 D693 외래 환자수는 12,345명입니다. [출처: HIRA]"

    client = Client()
    result = SourceResult(
        source="hira",
        query="D693 상병별 환자수 최근 5년",
        status="ok",
        payload={
            "calls": [
                {
                    "tool": "hira_disease_hospitalization_outpatient_stats",
                    "summary_text": "hira MCP returned totalCount=4",
                    "render_data": {
                        "items": [
                            {"year": "2024", "inpatient": "321", "outpatient": "12,345"}
                        ]
                    },
                }
            ]
        },
    )

    answer = V4Synthesizer(client).synthesize(_plan(), (result,), (), budget_s=15.0)

    prompt = client.messages[1]["content"]
    assert "2024" in prompt
    assert "12,345" in prompt
    assert prompt.index("external_evidence") < prompt.index("user_question")
    assert "12,345명" in answer


def test_v4_synthesizer_retries_internal_surface_once() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, _messages, *, budget_s=None, max_tokens=None) -> str:
            self.calls += 1
            if self.calls == 1:
                return "hira_disease_name_code MCP returned totalCount=1"
            return "D693 환자 통계는 HIRA 근거에서 확인되었습니다. [출처: HIRA]"

    result = SourceResult(
        source="hira",
        query="D693 환자수",
        status="ok",
        payload={"calls": [{"render_data": {"items": [{"year": "2024", "patients": "10"}]}}]},
    )

    answer = V4Synthesizer(Client()).synthesize(_plan(), (result,), (), budget_s=15.0)

    assert "MCP returned" not in answer
    assert "totalCount" not in answer
    assert "hira_disease_name_code" not in answer


def test_v4_synthesizer_replaces_repeated_internal_block_and_adds_hira_footnote() -> None:
    class Client:
        def complete(self, _messages, *, budget_s=None, max_tokens=None) -> str:
            return "설명입니다.\n\nhira_disease_name_code MCP returned totalCount=1"

    result = SourceResult(
        source="hira",
        query="D693 환자수",
        status="ok",
        payload={"calls": [{"render_data": {"items": [{"year": "2024", "patients": "10"}]}}]},
    )

    answer = V4Synthesizer(Client()).synthesize(_plan(), (result,), (), budget_s=15.0)

    assert "MCP returned" not in answer
    assert "totalCount" not in answer
    assert "hira_disease_name_code" not in answer
    assert "주상병 기준 청구 실인원" in answer


def test_v4_synthesizer_labels_scope_and_excludes_web_without_body() -> None:
    class Client:
        def __init__(self) -> None:
            self.prompt = ""

        def complete(self, messages, *, budget_s=None, max_tokens=None) -> str:
            self.prompt = messages[1]["content"]
            return "확인된 근거로 답변합니다."

    client = Client()
    web = SourceResult(
        source="web",
        query="최근 개정",
        status="ok",
        payload={"calls": [{"render_data": {"title": "로그인", "content": "짧음"}}]},
    )
    fda = SourceResult(
        source="openfda",
        query="리바로 안전성",
        status="ok",
        payload={"calls": [{"render_data": {"items": [{"drug": "Pitavastatin"}]}}]},
    )

    V4Synthesizer(client).synthesize(_plan(), (web, fda), (), budget_s=15.0)

    assert '"source_scope": "US"' in client.prompt
    assert '"source": "web"' not in client.prompt


def test_parallel_executor_calls_every_source_concurrently_and_reuses_session_cache() -> None:
    calls: list[tuple[str, str]] = []

    def adapter(source: str, query: str) -> SourceResult:
        calls.append((source, query))
        time.sleep(0.04)
        return SourceResult(
            source=source,
            query=query,
            status="ok",
            payload={"source": source, "query": query},
            citations=(
                Citation(
                    source=source,
                    query=query,
                    url=f"https://example.test/{source}",
                    retrieved_at=datetime.now(UTC),
                    used=False,
                ),
            ),
        )

    executor = ParallelSourceExecutor(
        adapters={name: (lambda query, source=name: adapter(source, query)) for name in SOURCE_NAMES},
        per_tool_timeout_s=1.0,
        total_timeout_s=2.0,
    )
    started = time.monotonic()
    first = executor.execute(_plan(), session_id="session-a")
    elapsed = time.monotonic() - started
    second = executor.execute(_plan(), session_id="session-a")

    assert elapsed < 0.18
    assert {item.source for item in first} == set(SOURCE_NAMES)
    assert len(calls) == 7
    assert all(item.cache_hit for item in second)
    assert len(calls) == 7


def test_parallel_executor_starts_each_source_before_extra_queries() -> None:
    calls: list[str] = []

    def adapter(source: str, query: str) -> SourceResult:
        calls.append(source)
        time.sleep(0.04)
        return SourceResult(source=source, query=query, status="ok", payload={"value": source})

    executor = ParallelSourceExecutor(
        adapters={name: (lambda query, source=name: adapter(source, query)) for name in SOURCE_NAMES},
        per_tool_timeout_s=0.08,
        total_timeout_s=0.2,
    )
    results = executor.execute(
        _plan(mart=tuple(f"mart query {index}" for index in range(8))),
        session_id="session-round-robin",
    )

    assert set(calls[:7]) == set(SOURCE_NAMES)
    assert {item.source for item in results if item.status == "ok"} >= set(SOURCE_NAMES)


def test_parallel_executor_marks_timeout_without_blocking_other_sources() -> None:
    def slow(query: str) -> SourceResult:
        time.sleep(0.2)
        return SourceResult(source="hira", query=query, status="ok", payload={})

    adapters = {
        name: (
            slow
            if name == "hira"
            else lambda query, source=name: SourceResult(
                source=source,
                query=query,
                status="ok",
                payload={"value": source},
            )
        )
        for name in SOURCE_NAMES
    }
    executor = ParallelSourceExecutor(
        adapters=adapters,
        per_tool_timeout_s=0.03,
        total_timeout_s=0.15,
    )

    results = executor.execute(_plan(), session_id="session-timeout")

    hira = next(item for item in results if item.source == "hira")
    assert hira.status == "timeout"
    assert hira.notice == "응답 지연으로 미포함"
    assert sum(item.status == "ok" for item in results) == 6

    gated = apply_v4_gates("질문", "확인된 답변", results)
    assert "응답 지연으로 미포함: hira" in gated.text


def test_invalid_planner_json_falls_back_to_all_seven_sources() -> None:
    class InvalidClient:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, _messages, *, budget_s=None) -> str:
            self.calls += 1
            return "not-json"

    client = InvalidClient()
    output = V4Planner(client).plan("리바로 요즘 어때", ())

    assert client.calls == 2
    assert {name for name, _queries in output.tool_queries.items()} == set(SOURCE_NAMES)
    assert all(queries for _name, queries in output.tool_queries.items())


def test_v4_clients_use_their_scoped_genos_endpoints_and_tokens(monkeypatch) -> None:
    monkeypatch.setenv("GENOS_BASE_URL", "https://genos.example/api/gateway/rep/serving/163")
    monkeypatch.setenv("GENOS_SERVING_ID", "202")
    monkeypatch.setenv("GENOS_FINAL_SERVING_ID", "202")
    monkeypatch.setenv("GENOS_PLANNER_SERVING_ID", "190")
    monkeypatch.setenv("GENOS_BEARER_TOKEN", "common-token")
    monkeypatch.setenv("GENOS_FINAL_BEARER_TOKEN", "final-token")
    monkeypatch.setenv("GENOS_PLANNER_BEARER_TOKEN", "planner-token")

    planner = planner_client()._client
    synthesizer = synthesizer_client()._client

    assert planner.base_url.endswith("/serving/190")
    assert planner.token == "planner-token"
    assert planner.timeout_s == 18
    assert synthesizer.base_url.endswith("/serving/202")
    assert synthesizer.token == "final-token"


def test_v4_synthesizer_transport_sets_explicit_token_cap(monkeypatch) -> None:
    captured = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

        def iter_lines(self, *, decode_unicode):
            assert decode_unicode is True
            yield 'data: {"choices":[{"delta":{"content":"답변"}}]}'
            yield "data: [DONE]"

        def close(self) -> None:
            captured["closed"] = True

    def post(url, *, headers, json, stream, timeout):
        captured.update(url=url, headers=headers, json=json, stream=stream, timeout=timeout)
        return Response()

    monkeypatch.setattr(v4_llm.requests, "post", post)
    client = v4_llm.GenOSV4Client(
        base_url="https://genos.example/serving/202",
        token="scoped-token",
        model="gemini-3-flash-preview",
        timeout_s=15,
        total_budget_s=20,
    )

    answer = client.complete([{"role": "user", "content": "질문"}], max_tokens=1800)

    assert answer == "답변"
    assert captured["json"]["max_tokens"] == 1800
    assert captured["json"]["model"] == "gemini-3-flash-preview"
    assert captured["closed"] is True


def test_planner_does_not_outer_retry_transport_failures() -> None:
    class TimeoutClient:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, _messages, *, budget_s=None) -> str:
            self.calls += 1
            raise requests.Timeout("planner transport timed out")

    client = TimeoutClient()
    output = V4Planner(client).plan("리바로 요즘 어때", ())

    assert client.calls == 1
    assert output.linking_plan == "planner fallback; no second hop: planner transport timed out"


def test_runtime_marks_successful_citations_used() -> None:
    plan = _plan()

    class Planner:
        def plan(self, _question, _turns, *, budget_s):
            return plan

        def link(self, *_args, **_kwargs):
            return None

    class Executor:
        def execute(self, _plan, *, session_id, total_timeout_s):
            return (
                SourceResult(
                    source="web",
                    query="web query",
                    status="ok",
                    payload={"answer": "근거"},
                    citations=(
                        Citation(
                            source="web",
                            query="web query",
                            url="https://example.test/source",
                            retrieved_at=datetime.now(UTC),
                            used=False,
                        ),
                    ),
                ),
            )

    class Synthesizer:
        def synthesize(self, _plan, results, _turns, *, budget_s):
            assert results[0].citations[0].used is True
            return "근거 기반 답변"

    answer = V4Runtime(
        planner=Planner(),
        executor=Executor(),
        synthesizer=Synthesizer(),
    ).answer("질문", conversation_id="conversation-a", turns=())

    assert answer.trace["tool_results"][0]["citations"][0]["used"] is True


def test_runtime_reserves_planner_budget_and_reports_serving_without_fallback() -> None:
    plan = _plan()

    class Planner:
        serving_id = "190"

        def plan(self, _question, _turns, *, budget_s):
            assert budget_s >= 18.0
            return plan

        def link(self, *_args, **_kwargs):
            return None

    class Executor:
        def execute(self, _plan, *, session_id, total_timeout_s):
            return ()

    class Synthesizer:
        def synthesize(self, _plan, _results, _turns, *, budget_s):
            return "근거 기반 답변"

    answer = V4Runtime(
        planner=Planner(),
        executor=Executor(),
        synthesizer=Synthesizer(),
    ).answer("질문", conversation_id="conversation-planner", turns=())

    assert answer.trace["planner_serving"] == "190"
    assert answer.trace["fallback"] is False


def test_runtime_runs_at_most_one_linking_hop() -> None:
    first_plan = _plan().model_copy(update={"needs_second_hop": True})
    linked_plan = _plan(web=("linked entity query",))

    class Planner:
        link_calls = 0

        def plan(self, _question, _turns, *, budget_s):
            return first_plan

        def link(self, *_args, **_kwargs):
            self.link_calls += 1
            return linked_plan

    class Executor:
        calls = 0

        def execute(self, plan, *, session_id, total_timeout_s):
            self.calls += 1
            return (
                SourceResult(
                    source="web",
                    query=plan.tool_queries.web[0],
                    status="ok",
                    payload={"answer": plan.tool_queries.web[0]},
                ),
            )

    class Synthesizer:
        def synthesize(self, _plan, results, _turns, *, budget_s):
            assert len(results) == 2
            return "연결 결과"

    planner = Planner()
    executor = Executor()
    answer = V4Runtime(
        planner=planner,
        executor=executor,
        synthesizer=Synthesizer(),
    ).answer("질문", conversation_id="conversation-link", turns=())

    assert planner.link_calls == 1
    assert executor.calls == 2
    assert answer.trace["second_hop"] is not None


def test_v4_gates_keep_mart_numbers_copy_only_and_require_sources() -> None:
    results = (
        SourceResult(
            source="mart",
            query="리바로 매출",
            status="ok",
            payload={"brand": "리바로", "sales_eok": 85.87, "source": "UBIST"},
            citations=(
                Citation(
                    source="UBIST",
                    query="리바로 매출",
                    url="mart://ubist/brand-metric",
                    retrieved_at=datetime.now(UTC),
                    used=True,
                ),
            ),
        ),
    )
    answer = "리바로 매출은 99.99억원입니다."

    gated = apply_v4_gates("리바로 매출 알려줘", answer, results)

    assert "99.99" not in gated.text
    assert "85.87" in gated.text
    assert "## 출처" in gated.text
    assert gated.trace["mart_numeric_copy_only"]["blocked"] is True


def test_v4_gates_do_not_treat_unrelated_payload_numbers_as_rank_evidence() -> None:
    results = (
        SourceResult(
            source="mart",
            query="리바로 순위",
            status="ok",
            payload={"sales_eok": 85.87, "row_id": 1},
        ),
    )

    gated = apply_v4_gates("리바로 순위 알려줘", "리바로는 1위입니다.", results)

    assert "1위" not in gated.text
    assert gated.trace["mart_numeric_copy_only"]["blocked"] is True


def test_v4_gates_refuse_source_impersonation_and_cross_source_sum() -> None:
    results = (
        SourceResult(
            source="mart",
            query="리바로 IQVIA",
            status="ok",
            payload={"source": "UBIST", "sales_eok": 85.87},
        ),
    )
    impersonated = apply_v4_gates("리바로를 IQVIA 기준으로 보여줘", "85.87억원", results)
    summed = apply_v4_gates(
        "UBIST 랑 IQVIA 합쳐서 총매출 알려줘",
        "합계는 100억원입니다.",
        results,
    )

    assert "IQVIA 근거를 확보하지 못했습니다" in impersonated.text
    assert "합산하지 않습니다" in summed.text


class _FakeV4Runtime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, int]] = []

    def answer(self, question: str, *, conversation_id: str | None, turns) -> V4Answer:
        self.calls.append((question, conversation_id, len(turns)))
        return V4Answer(
            text="V4 자유 답변\n\n## 출처\n- mart",
            charts=(),
            sources=("mart",),
            trace={"v4": True},
            timing={"total_elapsed_ms": 1.0},
            conversation_id=conversation_id or "v4-conversation",
        )


def test_flag_on_chat_answer_bypasses_legacy_answer_and_finalizer(monkeypatch) -> None:
    runtime = _FakeV4Runtime()
    monkeypatch.setenv("V4_PLANNER", "on")
    monkeypatch.setattr(service_app, "_get_v4_runtime", lambda: runtime)

    def legacy_must_not_run(*_args, **_kwargs):
        raise AssertionError("legacy path was called")

    monkeypatch.setattr(service_app, "_answer_question", legacy_must_not_run)
    monkeypatch.setattr(service_app, "_compute_final_answer_with_query_spec", legacy_must_not_run)
    client = TestClient(service_app.create_app())

    response = client.post("/chat/answer", json={"question": "리바로 요즘 어때"})

    assert response.status_code == 200
    assert response.json()["text"].startswith("V4 자유 답변")
    assert runtime.calls == [("리바로 요즘 어때", None, 0)]


def test_flag_off_chat_answer_is_identical_to_legacy_route(monkeypatch) -> None:
    monkeypatch.setenv("V4_PLANNER", "off")
    monkeypatch.setattr(
        service_app,
        "_get_v4_runtime",
        lambda: (_ for _ in ()).throw(AssertionError("V4 imported while off")),
        raising=False,
    )

    class Agent:
        def __init__(self, *, external_mode: str = "live") -> None:
            self.external_mode = external_mode

        def answer(self, question: str, _documents=None, **_kwargs):
            return {"answer": f"legacy:{question}", "sources": [], "tool_calls": []}

    monkeypatch.setattr(
        service_app.GenosClient,
        "stream_answer",
        lambda _self, _question, result: iter((result["answer"],)),
    )
    app = service_app.create_app(agent_factory=lambda external_mode="live": Agent(external_mode=external_mode))
    response = TestClient(app).post("/chat/answer", json={"question": "기존 경로"})

    assert response.status_code == 200
    assert response.json()["text"] == "legacy:기존 경로"


def test_flag_on_chat_session_replays_v4_answer_over_existing_sse(monkeypatch) -> None:
    runtime = _FakeV4Runtime()
    recorded: list[str] = []

    class History:
        def recent_turns(self, _conversation_id: str, _limit: int):
            return ()

        def record_turn(self, **kwargs) -> None:
            recorded.append(kwargs["answer_text"])

    monkeypatch.setenv("V4_PLANNER", "on")
    monkeypatch.setattr(service_app, "_get_v4_runtime", lambda: runtime)
    client = TestClient(service_app.create_app(history_store=History()))

    accepted = client.post("/chat", json={"question": "리바로 요즘 어때"})
    streamed = client.get(
        "/chat/stream",
        params={"session_id": accepted.json()["session_id"]},
    )

    assert accepted.status_code == 200
    assert streamed.status_code == 200
    assert "V4 자유 답변" in streamed.text
    assert "event: done" in streamed.text
    assert recorded == ["V4 자유 답변\n\n## 출처\n- mart"]


def test_flag_on_live_stream_emits_progress_before_running_v4(monkeypatch) -> None:
    runtime = _FakeV4Runtime()
    monkeypatch.setenv("V4_PLANNER", "on")
    monkeypatch.setattr(service_app, "_get_v4_runtime", lambda: runtime)
    app = service_app.create_app()
    client = TestClient(app)

    with client.stream(
        "GET",
        "/chat/stream",
        params={"question": "리바로 요즘 어때"},
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert body.index("event: step") < body.index("V4 자유 답변")
    assert runtime.calls == [("리바로 요즘 어때", None, 0)]
