"""Observability of the deterministic plan, the failure reason, and the deferred stop.

Three things were produced by the pipeline but never reached the caller: which
deterministic contract ran, why a metric query failed, and the prescription
stop that a mixed request had already earned. Each one is asserted here through
the *public* projection, not through the internal dict that produced it.
"""

from __future__ import annotations

from typing import Any

from jw_chat_agent_poc import ChatAgent
from jw_chat_agent_poc.agent_loop.bq_contracts import BQ_CONTRACT_IDS
from jw_chat_agent_poc.agent_loop.loop import ToolUseAgent
from jw_chat_agent_poc.common import timing as timing_module
from jw_chat_agent_poc.resolver import BrandResolver
from jw_chat_agent_poc.service.app import compute_final_answer
from jw_chat_agent_poc.service.genos_client import GenosClient
from jw_chat_agent_poc.service.runtime_provenance import trace_envelope
from jw_chat_agent_poc.tools.metrics import MetricsTool
from jw_chat_agent_poc.tools.query_layer import (
    MartRecord,
    StaticStrategicMartReader,
    StrategicQueryLayer,
)


BQ_QUESTION = "리바로 IQVIA랑 UBIST 수치가 다른데 왜?"
STRUCTURED_QUESTION = "리바로 점유율 알려줘"
MIXED_QUESTION = "리바로 최근 매출과 처방 추이를 알려줘"
PRESCRIPTION_ONLY_QUESTION = "리바로 처방조제액 추이"
SALES_ONLY_QUESTION = "리바로 최근 매출 추이 알려줘"
PRESCRIPTION_STOP_MARKER = "미노출되어 확인할 수 없습니다"


def _layer(sources: tuple[str, ...] = ("ubist", "iqvia_nsa")) -> StrategicQueryLayer:
    records = tuple(
        MartRecord(
            ml_id="ml_006",
            brand_name="리바로",
            source=source,
            measure="sales",
            metric_history={
                "2026-04": {"raw_value": start, "ms": share, "source_status": "OK"},
                "2026-05": {"raw_value": end, "ms": share + 0.1, "source_status": "OK"},
            },
            channel_data={},
            specialty_data={},
            dimension_data={},
            by_dimension={"company": "JW중외제약", "molecule": "pitavastatin"},
        )
        for source, start, end, share in (
            ("ubist", 8_000_000_000.0, 8_100_000_000.0, 3.7),
            ("iqvia_nsa", 8_300_000_000.0, 8_500_000_000.0, 3.9),
        )
        if source in sources
    )
    return StrategicQueryLayer(reader=StaticStrategicMartReader(records))


def _agent(sources: tuple[str, ...] = ("ubist", "iqvia_nsa")) -> ToolUseAgent:
    layer = _layer(sources)
    return ToolUseAgent(
        metrics=MetricsTool(mode="fixture", query_layer=layer),
        resolver=BrandResolver(mode="fixture"),
        query_layer=layer,
    )


def _public_trace(question: str, result: dict[str, Any]) -> dict[str, Any]:
    return trace_envelope(
        question=question,
        result=result,
        answer=str(result.get("answer") or ""),
        charts=(),
        timing=result.get("timing") or {"stages": []},
        conversation_id="o-plan-observability",
    )


# ---------------------------------------------------------------- O-1


def test_public_trace_names_the_bq_contract_that_ran() -> None:
    result = _agent().answer(BQ_QUESTION)

    plan = _public_trace(BQ_QUESTION, result)["qa_trace"]["plan"]

    assert plan["family"] == "bq"
    assert plan["kind"] == "BQ:C3"
    assert plan["hit"] is True


def test_public_trace_names_the_structured_plan_that_ran() -> None:
    result = _agent().answer(STRUCTURED_QUESTION)

    plan = _public_trace(STRUCTURED_QUESTION, result)["qa_trace"]["plan"]

    assert plan["family"] == "structured"
    assert plan["kind"] == "brand_share"
    assert plan["hit"] is True


def test_public_trace_states_no_plan_as_null_rather_than_omitting_the_key() -> None:
    """"Not observed" and "not applicable" must stay distinguishable."""
    trace = _public_trace("설명해줘", {"tool_calls": [], "markdown_response": {}})

    plan = trace["qa_trace"]["plan"]

    assert plan["family"] == "none"
    assert plan["kind"] is None
    assert plan["hit"] is None
    assert plan["missing_sources"] is None


def test_public_trace_surfaces_the_sources_the_contract_could_not_reach() -> None:
    result = _agent(sources=("ubist",)).answer(BQ_QUESTION)

    plan = _public_trace(BQ_QUESTION, result)["qa_trace"]["plan"]

    assert plan["missing_sources"] == ["iqvia_nsa"]


def test_public_trace_reports_an_unregistered_contract_id_as_other() -> None:
    result = {
        "tool_calls": [],
        "markdown_response": {},
        "agent_loop_metrics": {
            "deterministic_plan_hit": True,
            "deterministic_plan_kind": "BQ:NOT_A_CONTRACT",
            "bq_missing_sources": [],
        },
    }

    plan = _public_trace("설명해줘", result)["qa_trace"]["plan"]

    assert plan["family"] == "bq"
    assert plan["kind"] == "other"


def test_bq_and_structured_planning_are_separate_spans() -> None:
    bq_stages = _agent().answer(BQ_QUESTION)["timing"]["stages"]
    structured_stages = _agent().answer(STRUCTURED_QUESTION)["timing"]["stages"]

    bq_names = {stage["name"] for stage in bq_stages}
    structured_names = {stage["name"] for stage in structured_stages}

    assert bq_names & {"deterministic_plan_bq"}
    assert structured_names & {"deterministic_plan_structured"}
    assert not bq_names & structured_names & {
        "deterministic_plan_bq",
        "deterministic_plan_structured",
    }


def test_new_planning_spans_keep_a_user_facing_label() -> None:
    events: list[dict[str, Any]] = []
    for name in ("deterministic_plan_bq", "deterministic_plan_structured"):
        with timing_module.stage(None, name, "브랜드=리바로", sink=events.append):
            pass

    started = events[::2]

    assert [event["name"] for event in started] == ["조회 계획 확정", "조회 계획 확정"]
    public_text = str([{"name": event["name"], "detail": event["detail"]} for event in started])
    assert "deterministic_plan_bq" not in public_text
    assert "deterministic_plan_structured" not in public_text


# ---------------------------------------------------------------- O-2


def _query_failed_result() -> dict[str, Any]:
    return {
        "markdown_response": {},
        "tool_calls": [
            {
                "tool": "query_failed",
                "status": "error",
                "render_data": {
                    "brand": "아일리아",
                    "metric": "competition",
                    "status": "query_failed",
                    "message": "조회에 실패했습니다.",
                    "error_type": "LookupError",
                    "reason_code": "record_absent",
                    "tool_name": "get_brand_metric",
                },
            }
        ],
    }


def test_public_trace_carries_the_query_failure_reason_code() -> None:
    trace = _public_trace("아일리아 경쟁 약물 현황 알려줘", _query_failed_result())

    tool = trace["qa_trace"]["tools"][0]

    assert tool["reason_code"] == "record_absent"


def test_public_trace_states_a_missing_reason_code_as_null() -> None:
    result = {
        "markdown_response": {},
        "tool_calls": [{"tool": "get_brand_metric", "status": "ok", "render_data": {}}],
    }

    tool = _public_trace("리바로 매출", result)["qa_trace"]["tools"][0]

    assert "reason_code" in tool
    assert tool["reason_code"] is None


def test_public_trace_rejects_a_reason_code_outside_the_registry() -> None:
    result = _query_failed_result()
    result["tool_calls"][0]["render_data"]["reason_code"] = "제조사 내부 커넥션 문자열"

    tool = _public_trace("아일리아", result)["qa_trace"]["tools"][0]

    assert tool["reason_code"] == "other"


# ---------------------------------------------------------------- O-3


def _stub_stream(monkeypatch, text: str) -> None:
    def stream_answer(_self: GenosClient, _question: str, _result: dict):
        yield text

    monkeypatch.setattr(GenosClient, "stream_answer", stream_answer)


def test_mixed_request_keeps_the_prescription_stop_in_the_delivered_body(monkeypatch) -> None:
    result = ChatAgent(external_mode="fixture").answer(MIXED_QUESTION)
    assert result["prescription_metric_deferred"] is not None
    _stub_stream(monkeypatch, "리바로 매출은 최근 구간에서 완만하게 늘었습니다.")

    final = compute_final_answer(MIXED_QUESTION, result, "o3-mixed")

    assert "매출" in final.text
    assert PRESCRIPTION_STOP_MARKER in final.text
    assert "매출 지표로 대체하지 않습니다" in final.text
    assert "조회 계약" not in final.text
    assert "null" not in final.text
    assert "조회 범위" in final.text
    assert "확인 불가" in final.text


def test_the_stop_is_appended_once_even_when_the_body_already_carries_it(monkeypatch) -> None:
    result = ChatAgent(external_mode="fixture").answer(MIXED_QUESTION)
    _stub_stream(monkeypatch, str(result["answer"]))

    final = compute_final_answer(MIXED_QUESTION, result, "o3-once")

    assert final.text.count(PRESCRIPTION_STOP_MARKER) == 1


def test_a_sales_only_request_gains_no_prescription_stop(monkeypatch) -> None:
    result = ChatAgent(external_mode="fixture").answer(SALES_ONLY_QUESTION)
    assert result.get("prescription_metric_deferred") is None
    _stub_stream(monkeypatch, "리바로 매출은 최근 구간에서 완만하게 늘었습니다.")

    final = compute_final_answer(SALES_ONLY_QUESTION, result, "o3-sales")

    assert PRESCRIPTION_STOP_MARKER not in final.text


def test_a_prescription_only_request_still_returns_the_stop_alone(monkeypatch) -> None:
    result = ChatAgent(external_mode="fixture").answer(PRESCRIPTION_ONLY_QUESTION)
    _stub_stream(monkeypatch, str(result.get("answer") or ""))

    final = compute_final_answer(PRESCRIPTION_ONLY_QUESTION, result, "o3-rx")

    assert PRESCRIPTION_STOP_MARKER in final.text
    assert final.text.count(PRESCRIPTION_STOP_MARKER) == 1


# ---------------------------------------------------------------- leak guard


def test_the_public_plan_projection_carries_no_free_text() -> None:
    result = _agent().answer(BQ_QUESTION)

    plan = _public_trace(BQ_QUESTION, result)["qa_trace"]["plan"]

    assert set(plan) == {"family", "kind", "hit", "missing_sources"}
    assert plan["family"] in {"bq", "structured", "none"}
    assert plan["kind"] is None or plan["kind"] == "other" or plan["kind"].removeprefix(
        "BQ:"
    ) in set(BQ_CONTRACT_IDS)
    assert all(source in {"ubist", "iqvia_nsa"} for source in plan["missing_sources"] or ())
