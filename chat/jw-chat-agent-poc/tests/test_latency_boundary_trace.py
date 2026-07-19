from __future__ import annotations

from types import SimpleNamespace

from jw_chat_agent_poc.agent_loop.planner import GenosToolPlanner
from jw_chat_agent_poc.agent_loop.structured_planner import preflight_structured_market_question
from jw_chat_agent_poc.common.timing import request_span_scope
from jw_chat_agent_poc.resolver import BrandResolver
from jw_chat_agent_poc.service import app as service_app


class _Resolver:
    def resolve_many(self, question: str):
        return ()

    def resolve(self, question: str, *, allow_default: bool):
        return None


class _Dependencies:
    def __init__(self) -> None:
        self.resolver = _Resolver()
        self.router = SimpleNamespace(route=lambda question, has_documents: ())

    def agent_loop_dependencies(self):
        return object()


class _Agent:
    def answer(self, question: str) -> dict:
        return {
            "question": question,
            "tool_calls": [],
            "sources": [],
            "answer": "ok",
            "markdown_response": {"markdown": "ok", "fact_md": "", "data_md": ""},
        }


def test_direct_agent_loop_records_classification_boundary_children(monkeypatch) -> None:
    dependencies = _Dependencies()
    monkeypatch.setattr(
        service_app,
        "build_chat_agent_dependencies",
        lambda *, external_mode: dependencies,
    )
    monkeypatch.setattr(
        service_app,
        "preflight_structured_market_question",
        lambda question, resolver: object(),
    )
    monkeypatch.setattr(
        service_app,
        "build_tool_use_agent",
        lambda agent_dependencies: _Agent(),
    )

    with request_span_scope() as spans:
        service_app._answer_direct_agent_loop("리바로 2025년 2분기 매출", "live")

    names = [span["name"] for span in spans]
    assert names == [
        "question_classification",
        "structured_preflight",
        "metric_owner_resolution",
        "canonical_brand_resolution",
        "agent_loop_construction",
        "agent_loop_execution",
    ]
    assert all(span["status"] == "ok" for span in spans)
    assert all(span["ended_at"] >= span["started_at"] for span in spans)


def test_genos_planner_records_prepare_http_wait_and_decode_spans(monkeypatch) -> None:
    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "choices": [{"message": {"content": "도구 결과로 답변하세요."}}],
                "usage": {},
            }

    monkeypatch.setattr("requests.post", lambda *_args, **_kwargs: _Response())
    planner = GenosToolPlanner(base_url="https://planner.example", token="token")

    with request_span_scope() as spans:
        decision = planner._request_decision("질문", (), (), (), ())

    assert decision.final_answer == "도구 결과로 답변하세요."
    assert [span["name"] for span in spans] == [
        "planner_request_prepare",
        "planner_http_wait",
        "planner_response_decode",
    ]


def test_structured_preflight_exposes_catalog_and_duplicate_resolution_costs() -> None:
    resolver = BrandResolver(mode="fixture")

    with request_span_scope() as spans:
        plan = preflight_structured_market_question("리바로 2025년 2분기 매출", resolver)

    assert plan is not None
    names = [span["name"] for span in spans]
    assert names.count("brand_catalog_load") == 2
    assert names.count("brand_catalog_assembly") == 1
    assert names.count("brand_alias_match_many") == 2
    assert "period_grounding" in names
    assert "tool_schema_catalog" in names
    assert "structured_plan_assembly" in names
