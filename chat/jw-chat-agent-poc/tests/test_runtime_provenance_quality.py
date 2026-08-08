from __future__ import annotations

from datetime import datetime

from jw_chat_agent_poc.agent_loop.tools import AgentToolFacade, ToolExecution
from jw_chat_agent_poc.orchestrator.answer_facts import answer_fact_markdown
from jw_chat_agent_poc.service.markdown_cleanup import cleanup_markdown_answer
from jw_chat_agent_poc.service.runtime_provenance import _empty_result_calls, _ungrounded_numbers, trace_envelope
from jw_chat_agent_poc.service.web_mi_summary import web_search_mi_section_from_calls


def test_recovered_tool_call_is_not_reported_as_empty_result() -> None:
    result = {
        "tool_calls": [
            {"tool": "mfds_permission_search", "status": "error"},
            {
                "tool": "mfds_permission_search",
                "status": "ok",
                "render_data": {
                    "ok": True,
                    "evidence": [{"subject": "리바로", "metric": "허가 품목"}],
                },
            },
        ]
    }

    assert _empty_result_calls(result) == ()


def test_unrecovered_tool_call_remains_empty_result() -> None:
    result = {
        "tool_calls": [
            {"tool": "mfds_permission_search", "status": "error"},
            {"tool": "web_search", "status": "ok", "render_data": {"ok": True, "evidence": [{}]}},
        ]
    }

    assert _empty_result_calls(result) == (
        {"tool": "mfds_permission_search", "status": "error"},
    )


def test_rendered_fact_number_is_grounded_when_allowed_numbers_is_incomplete() -> None:
    markdown_response = {
        "allowed_numbers": (),
        "fact_md": "| 기간 | 매출 |\n| --- | --- |\n| 2025-04 | 83.184115억원 |",
        "data_md": "",
    }

    assert _ungrounded_numbers("리바로 2025-04 매출은 83.184115억원입니다.", markdown_response) == ()


def test_exact_monthly_sales_is_grounded_by_successful_structured_tool_value() -> None:
    call = {
        "tool": "get_brand_metric",
        "status": "ok",
        "source": "UBIST",
        "render_data": {
            "status": "ok",
            "brand": "리바로",
            "metric": "sales",
            "period": "2025-04",
            "sales_억원": 83.184115,
            "sales_krw": 8_318_411_526.5,
        },
    }
    fact_md = answer_fact_markdown([call], ["UBIST"])
    markdown_response = {
        "allowed_numbers": (),
        "fact_md": fact_md,
        "data_md": fact_md,
    }

    assert "| 매출 | 83.18억원 |" in fact_md
    assert (
        _ungrounded_numbers(
            "2025-04 리바로 매출은 83.184115억원입니다.",
            markdown_response,
            [call],
        )
        == ()
    )


def test_successful_execution_preserves_status_for_runtime_numeric_grounding() -> None:
    call = {
        "tool": "get_brand_metric",
        "source": "UBIST",
        "render_data": {
            "brand": "리바로",
            "metric": "sales",
            "period": "2025-04",
            "sales_억원": 83.184115,
            "sales_krw": 8_318_411_526.5,
        },
    }

    execution = ToolExecution("ok", "리바로 sales query-layer", call, {"period": "2025-04"})

    assert execution.call["status"] == "ok"
    assert (
        _ungrounded_numbers(
            "2025-04 리바로 매출은 83.184115억원입니다.",
            {"allowed_numbers": (), "fact_md": "", "data_md": ""},
            [execution.call],
        )
        == ()
    )


def test_tool_execution_records_minimal_qa_timestamps_and_status() -> None:
    facade = object.__new__(AgentToolFacade)

    execution = facade.execute("unsupported_fixture_tool", {})

    qa_trace = execution.call["qa_trace"]
    started = datetime.fromisoformat(qa_trace["started_at"])
    ended = datetime.fromisoformat(qa_trace["ended_at"])
    assert started.tzinfo is not None
    assert ended >= started
    assert qa_trace["status"] == "unsupported"
    assert qa_trace["row_count"] == 0
    assert qa_trace["data_as_of"] is None
    assert qa_trace["cache_hit"] is False


def test_trace_envelope_projects_request_route_tool_claim_and_final_qa_fields(monkeypatch) -> None:
    monkeypatch.setenv("HOSTNAME", "chat-pod-fixture")
    monkeypatch.setenv("JW_CHAT_GIT_SHA", "candidate-sha")
    result = {
        "context_scope": "MARKET",
        "router_diagnostics": {"mode": "tool_use_agent", "reason": "structured_metric_owner"},
        "tool_calls": [
            {
                "tool": "query_failed",
                "status": "query_failed",
                "render_data": {"tool_name": "get_brand_share", "status": "query_failed"},
                "qa_trace": {
                    "started_at": "2026-07-19T00:00:00+00:00",
                    "ended_at": "2026-07-19T00:00:01+00:00",
                    "status": "query_failed",
                    "row_count": 0,
                    "data_as_of": None,
                    "cache_hit": False,
                    "endpoint": "/api/cause/%EB%A6%AC%EB%B0%94%EB%A1%9C",
                    "latency_ms": 1000.0,
                    "source_epoch": "epoch-20260720",
                    "built_at": "2026-07-20T00:00:00Z",
                },
            }
        ],
        "_qa_claim_gate": {
            "blocked_claim_count": 1,
            "blocked_reasons": ["missing_share_evidence"],
            "disposition": "unavailable",
        },
        "markdown_response": {"fact_md": "", "data_md": ""},
    }

    trace = trace_envelope(
        question="자누비아 점유율",
        result=result,
        answer="상태: 확인 불가",
        charts=(),
        timing={"stages": []},
        conversation_id="qa-session",
    )

    qa_trace = trace["qa_trace"]
    assert qa_trace["request"] == {
        "request_id": trace["trace_id"],
        "session_id": "qa-session",
        "pod": "chat-pod-fixture",
        "image_revision": "candidate-sha",
    }
    assert qa_trace["routing"]["scope"] == "MARKET"
    assert qa_trace["routing"]["gate"] == "tool_use_agent"
    assert qa_trace["routing"]["gate_reason"] == "structured_metric_owner"
    assert qa_trace["tools"][0]["name"] == "get_brand_share"
    assert qa_trace["tools"][0]["status"] == "query_failed"
    assert qa_trace["tools"][0]["endpoint"] == "/api/cause/%EB%A6%AC%EB%B0%94%EB%A1%9C"
    assert qa_trace["tools"][0]["latency_ms"] == 1000.0
    assert qa_trace["tools"][0]["source_epoch"] == "epoch-20260720"
    assert qa_trace["tools"][0]["built_at"] == "2026-07-20T00:00:00Z"
    assert qa_trace["claims"] == {
        "blocked_count": 1,
        "blocked_reasons": ("missing_share_evidence",),
    }
    assert qa_trace["final"] == {
        "disposition": "unavailable",
        "body_empty": False,
        "failure_kind": "tool_error",
    }


def test_trace_envelope_projects_only_safe_answer_control_metadata(monkeypatch) -> None:
    monkeypatch.setenv("HOSTNAME", "chat-pod-fixture")
    result = {
        "_answer_control_layer": {
            "applied": True,
            "intent": "SOURCE_DIFFERENCE",
            "required_slot_coverage": "4/4",
            "question_spec_sha256": "a" * 64,
            "claim_plan_sha256": "b" * 64,
            "evidence_set_sha256": "c" * 64,
            "selected_branch": "answer_projection",
            "degraded": False,
            "claim_plan": ("must_not_be_public",),
        },
        "tool_calls": [],
        "markdown_response": {},
    }

    trace = trace_envelope(
        question="IQVIA랑 UBIST 수치가 다른데 왜?",
        result=result,
        answer="source contract",
        charts=(),
        timing={"stages": []},
        conversation_id="qa-session",
    )

    assert trace["answer_control_layer"] == {
        "applied": True,
        "intent": "SOURCE_DIFFERENCE",
        "required_slot_coverage": "4/4",
        "question_spec_sha256": "a" * 64,
        "claim_plan_sha256": "b" * 64,
        "evidence_set_sha256": "c" * 64,
        "selected_branch": "answer_projection",
        "degraded": False,
    }
    assert "claim_plan" not in trace["answer_control_layer"]


def test_trace_envelope_preserves_explicit_typed_gate_decision(monkeypatch) -> None:
    monkeypatch.setenv("HOSTNAME", "chat-pod-fixture")
    monkeypatch.setenv("JW_CHAT_GIT_SHA", "candidate-sha")
    result = {
        "router_diagnostics": {
            "mode": "deterministic",
            "scope": "market_membership_mismatch",
            "gate": "brand_market_membership",
            "gate_reason": "explicit_market_outside_brand_memberships",
        },
        "tool_calls": [],
        "markdown_response": {"fact_md": "", "data_md": ""},
    }

    trace = trace_envelope(
        question="고지혈증 시장에서 마운자로 점유율",
        result=result,
        answer="마운자로는 요청한 고지혈증 시장에 포함되지 않습니다.",
        charts=(),
        timing={"stages": []},
        conversation_id="qa-membership-session",
    )

    routing = trace["qa_trace"]["routing"]
    assert routing["scope"] == "market_membership_mismatch"
    assert routing["gate"] == "brand_market_membership"
    assert routing["gate_reason"] == "explicit_market_outside_brand_memberships"


def test_trace_envelope_projects_request_child_spans() -> None:
    result = {
        "router_diagnostics": {"mode": "tool_use_agent"},
        "tool_calls": [],
        "markdown_response": {"fact_md": "", "data_md": ""},
        "_qa_spans": [
            {
                "name": "structured_preflight",
                "category": "boundary",
                "detail": "deterministic structured question preflight",
                "started_at": "2026-07-20T00:00:00+00:00",
                "ended_at": "2026-07-20T00:00:01+00:00",
                "elapsed_ms": 1000.0,
                "status": "ok",
            }
        ],
    }

    trace = trace_envelope(
        question="리바로 2025년 2분기 매출",
        result=result,
        answer="2025-Q2 리바로 매출은 242.72억원입니다.",
        charts=(),
        timing={"stages": []},
        conversation_id="qa-boundary-session",
    )

    assert trace["qa_trace"]["spans"] == (
        {
            "name": "structured_preflight",
            "category": "boundary",
            "detail": "deterministic structured question preflight",
            "started_at": "2026-07-20T00:00:00+00:00",
            "ended_at": "2026-07-20T00:00:01+00:00",
            "elapsed_ms": 1000.0,
            "status": "ok",
        },
    )


def test_number_absent_from_rendered_facts_remains_ungrounded() -> None:
    markdown_response = {
        "allowed_numbers": (),
        "fact_md": "| 기간 | 매출 |\n| --- | --- |\n| 2025-04 | 83.184115억원 |",
        "data_md": "",
    }

    assert _ungrounded_numbers("리바로 매출은 99.99억원입니다.", markdown_response) == ("99.99억원",)


def test_public_web_search_number_cannot_ground_generated_numeric_claim() -> None:
    markdown_response = {
        "allowed_numbers": (),
        "fact_md": "",
        "data_md": "",
    }
    tool_calls = [
        {
            "tool": "web_search",
            "status": "ok",
            "render_data": {
                "items": [
                    {
                        "title": "가이드라인 업데이트",
                        "url": "https://example.test/guideline",
                        "snippet": "고위험군에서 LDL-C를 30% 이상 낮추도록 권고합니다.",
                    }
                ]
            },
        }
    ]

    assert _ungrounded_numbers(
        "웹 검색 근거에서는 LDL-C를 30% 이상 낮추도록 권고합니다.",
        markdown_response,
        tool_calls,
    ) == ("30%",)


def test_live_public_web_search_numbers_remain_supplementary() -> None:
    # Given: the live external adapter returned public web evidence.
    markdown_response = {
        "allowed_numbers": (),
        "fact_md": "",
        "data_md": "",
    }
    tool_calls = [
        {
            "tool": "web_search",
            "status": "live",
            "render_data": {
                "items": [
                    {
                        "title": "2023-06-20 가이드라인 업데이트",
                        "url": "https://example.test/guideline",
                        "snippet": "고위험군에서 LDL-C를 30% 이상 낮추도록 권고합니다.",
                    }
                ]
            },
        }
    ]

    # When: the runtime grounding gate checks the answer.
    ungrounded = _ungrounded_numbers(
        "2023-06-20 지침은 LDL-C를 30% 이상 낮추도록 권고합니다.",
        markdown_response,
        tool_calls,
    )

    # Then: web dates and values cannot become authoritative answer evidence.
    assert ungrounded == ("-06", "-20", "2023", "30%")


def test_partial_public_web_evidence_cannot_ground_generated_numeric_claim() -> None:
    # Given: one external source returned evidence while another returned no data.
    markdown_response = {
        "allowed_numbers": (),
        "fact_md": "",
        "data_md": "",
    }
    tool_calls = [
        {
            "tool": "web_search",
            "source": "web_search",
            "status": "partial",
            "render_data": {
                "calls": [
                    {
                        "tool": "web_search",
                        "status": "live",
                        "render_data": {
                            "items": [
                                {
                                    "title": "가이드라인 업데이트",
                                    "url": "https://example.test/guideline",
                                    "snippet": "LDL-C 목표를 28% 낮춘 결과를 보고했습니다.",
                                }
                            ]
                        },
                    },
                    {
                        "tool": "web_search",
                        "status": "no_data",
                        "render_data": {"items": []},
                    },
                ]
            },
        }
    ]

    # When: the runtime grounding gate checks a value from the successful source.
    ungrounded = _ungrounded_numbers(
        "확인된 공개 근거에서는 LDL-C 목표가 28% 낮아졌습니다.",
        markdown_response,
        tool_calls,
    )

    # Then: partial web evidence remains a separate appendix, not numeric grounding.
    assert ungrounded == ("28%",)


def test_non_rendered_tool_internal_number_remains_ungrounded() -> None:
    markdown_response = {
        "allowed_numbers": (),
        "fact_md": "",
        "data_md": "",
    }
    tool_calls = [
        {
            "tool": "web_search",
            "status": "ok",
            "render_data": {
                "items": [
                    {
                        "title": "가이드라인 업데이트",
                        "url": "https://example.test/guideline",
                        "snippet": "고위험군 치료 권고를 정리했습니다.",
                    }
                ],
                "internal_total_count": 999,
            },
        }
    ]

    assert _ungrounded_numbers("검색 내부 건수는 999건입니다.", markdown_response, tool_calls) == ("999건",)


def test_live_tool_internal_number_remains_ungrounded() -> None:
    # Given: a live call includes one public item and an internal-only counter.
    markdown_response = {
        "allowed_numbers": (),
        "fact_md": "",
        "data_md": "",
    }
    tool_calls = [
        {
            "tool": "web_search",
            "status": "live",
            "render_data": {
                "items": [
                    {
                        "title": "가이드라인 업데이트",
                        "url": "https://example.test/guideline",
                        "snippet": "고위험군 치료 권고를 정리했습니다.",
                    }
                ],
                "internal_total_count": 999,
            },
        }
    ]

    # When: the answer cites the internal-only counter.
    ungrounded = _ungrounded_numbers("검색 내부 건수는 999건입니다.", markdown_response, tool_calls)

    # Then: live status does not expose fields outside the public projection.
    assert ungrounded == ("999건",)


def test_deterministic_web_appendix_is_excluded_from_claim_grounding() -> None:
    markdown_response = {
        "allowed_numbers": (),
        "fact_md": "",
        "data_md": "",
    }
    tool_calls = [
        {
            "tool": "web_search",
            "status": "live",
            "render_data": {
                "items": [
                    {
                        "title": "과거 허가 기사",
                        "url": "https://www.biospectator.com/news/view/27271",
                        "snippet": "리바로 허가 이력을 정리한 기사입니다.",
                        "published_date": "2016-05-20",
                    }
                ]
            },
        }
    ]
    answer = "\n\n".join(
        (
            "시장 수치는 99.99억원입니다.",
            web_search_mi_section_from_calls(tool_calls),
        )
    )

    assert _ungrounded_numbers(answer, markdown_response, tool_calls) == ("99.99억원",)


def test_cleaned_deterministic_web_appendix_is_excluded_from_claim_grounding() -> None:
    markdown_response = {
        "allowed_numbers": (),
        "fact_md": "",
        "data_md": "",
    }
    tool_calls = [
        {
            "tool": "web_search",
            "status": "live",
            "render_data": {
                "items": [
                    {
                        "title": "리바로 1위브랜드 허가 기사",
                        "url": "https://www.biospectator.com/news/view/27271",
                        "snippet": "리바로 허가 이력을 정리한 기사입니다.",
                        "published_date": "2016-05-20",
                    }
                ]
            },
        }
    ]
    answer = cleanup_markdown_answer(
        "\n\n".join(
            (
                "시장 수치는 99.99억원입니다.",
                web_search_mi_section_from_calls(tool_calls),
            )
        )
    )

    assert _ungrounded_numbers(answer, markdown_response, tool_calls) == ("99.99억원",)


def test_question_aware_web_appendix_is_excluded_from_claim_grounding() -> None:
    question = "상병코드 D693의 환자수 추이를 알려줘"
    markdown_response = {
        "allowed_numbers": (),
        "fact_md": "",
        "data_md": "",
    }
    tool_calls = [
        {
            "tool": "hira_disease_hospitalization_outpatient_stats",
            "source": "HIRA",
            "status": "error",
            "render_data": {"error_code": "UPSTREAM_UNAVAILABLE"},
        },
        {
            "tool": "web_search",
            "status": "live",
            "render_data": {
                "items": [
                    {
                        "title": "HIRA 통계 안내",
                        "url": "https://opendata.hira.or.kr/guide",
                        "snippet": "공식 통계 시스템 이용 안내입니다.",
                        "published_date": "2025-01-02",
                    }
                ]
            },
        },
    ]
    answer = "\n\n".join(
        (
            "시장 수치는 99.99억원입니다.",
            web_search_mi_section_from_calls(tool_calls, question=question),
        )
    )

    assert _ungrounded_numbers(
        answer,
        markdown_response,
        tool_calls,
        question=question,
    ) == ("99.99억원",)


def test_web_appendix_heading_alone_does_not_bypass_claim_grounding() -> None:
    markdown_response = {
        "allowed_numbers": (),
        "fact_md": "",
        "data_md": "",
    }
    tool_calls = [
        {
            "tool": "web_search",
            "status": "live",
            "render_data": {
                "items": [
                    {
                        "title": "리바로 허가 기사",
                        "url": "https://example.test/article",
                        "snippet": "리바로 허가 이력을 정리한 기사입니다.",
                        "published_date": "2016-05-20",
                    }
                ]
            },
        }
    ]
    answer = "### 웹 검색 결과(미검증)\n\n시장 수치는 99.99억원입니다."

    assert _ungrounded_numbers(answer, markdown_response, tool_calls) == ("99.99억원",)


def test_number_repeated_in_narrative_remains_ungrounded_even_when_web_appendix_contains_it() -> None:
    markdown_response = {
        "allowed_numbers": (),
        "fact_md": "",
        "data_md": "",
    }
    tool_calls = [
        {
            "tool": "web_search",
            "status": "live",
            "render_data": {
                "items": [
                    {
                        "title": "과거 허가 기사",
                        "url": "https://www.biospectator.com/news/view/27271",
                        "snippet": "리바로 허가 이력을 정리한 기사입니다.",
                        "published_date": "2016-05-20",
                    }
                ]
            },
        }
    ]
    answer = "\n\n".join(
        (
            "기사 식별자 27271이 핵심 시장 수치입니다.",
            web_search_mi_section_from_calls(tool_calls),
        )
    )

    assert _ungrounded_numbers(answer, markdown_response, tool_calls) == ("27271",)


def test_bare_url_identifiers_are_not_treated_as_numeric_claims() -> None:
    markdown_response = {
        "allowed_numbers": (),
        "fact_md": "",
        "data_md": "",
    }
    answer = (
        "| 기사 | URL |\n"
        "| --- | --- |\n"
        "| 과거 허가 기사 | https://example.test/news/2016/27271-20?item=3300 |"
    )

    assert _ungrounded_numbers(answer, markdown_response) == ()


def test_visible_number_remains_ungrounded_when_same_number_appears_in_bare_url() -> None:
    markdown_response = {
        "allowed_numbers": (),
        "fact_md": "",
        "data_md": "",
    }
    answer = "\n".join(
        (
            "기사 식별자 27271이 핵심 시장 수치입니다.",
            "원문: https://example.test/news/2016/27271-20?item=3300",
        )
    )

    assert _ungrounded_numbers(answer, markdown_response) == ("27271",)


def test_trace_envelope_marks_web_only_numeric_claim_ungrounded() -> None:
    result = {
        "context_scope": "MARKET",
        "tool_calls": [
            {
                "tool": "web_search",
                "status": "ok",
                "render_data": {
                    "items": [
                        {
                            "title": "가이드라인 업데이트",
                            "url": "https://example.test/guideline",
                            "snippet": "고위험군에서 LDL-C를 30% 이상 낮추도록 권고합니다.",
                        }
                    ]
                },
            }
        ],
        "markdown_response": {
            "allowed_numbers": (),
            "fact_md": "",
            "data_md": "",
        },
    }

    trace = trace_envelope(
        question="/deep 고지혈증 치료 가이드라인",
        result=result,
        answer="웹 검색 근거에서는 LDL-C를 30% 이상 낮추도록 권고합니다.",
        charts=(),
        timing={"stages": []},
        conversation_id="fixture",
    )

    assert trace["ungrounded_numeric_spans"] == ("30%",)
