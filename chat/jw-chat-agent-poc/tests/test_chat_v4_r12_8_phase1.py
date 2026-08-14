from __future__ import annotations

from datetime import date

from jw_chat_agent_poc.service.v4 import adapters, planner
from jw_chat_agent_poc.service.v4.contracts import (
    PlannerOutput,
    RequestedAnswerShape,
    SourceResult,
    ToolQueries,
)
from jw_chat_agent_poc.service.v4.expansion import expand_parameter_axes
from jw_chat_agent_poc.service.v4.gates import apply_v4_gates
from jw_chat_agent_poc.service.v4.query_scope import (
    DEFAULT_ENTITY_LIMIT,
    HARD_ENTITY_LIMIT,
    SOURCE_CALL_LIMIT,
    apply_source_call_cap,
    configured_entity_limit,
    classify_upstream_failure,
    redact_failure_body,
)
from jw_chat_agent_poc.service.v4.retrieval_events import (
    public_retrieval_notice,
    retrieval_event_from_result,
)
from jw_chat_agent_poc.service.v4.runtime import _query_scope_notice
from jw_chat_agent_poc.service.v4.runtime import (
    _bind_session_state_contract,
    _derive_session_state,
    _execution_plan,
    _gap_fill_request,
    _session_inheritance_notice,
)
from jw_chat_agent_poc.service.v4.session_state import SessionState
from jw_chat_agent_poc.service.v4.synthesizer import _hira_patient_facts


def _plan(
    question: str,
    *,
    entities: tuple[str, ...] = (),
    answer_sources: tuple[str, ...] = ("hira",),
) -> PlannerOutput:
    return PlannerOutput(
        resolved_question=question,
        expanded_intents=(question,),
        answer_sources=answer_sources,
        tool_queries=ToolQueries(
            mart=(question,),
            nedrug=(question,),
            hira=(question,),
            openfda=(question,),
            clinicaltrials=(question,),
            web=(question,),
            patent=(question,),
        ),
        linking_plan="deterministic",
        requested_answer_shape=RequestedAnswerShape(
            entities=entities,
            measure_or_attribute=("patient_count",),
        ),
    )


def test_q1_planner_entities_survive_lossless_contract_attachment() -> None:
    plan = _plan("당뇨병 환자수 알려줘", entities=("E10", "E11", "E12", "E13", "E14"))

    attached = planner._attach_lossless_contracts("당뇨병 환자수 알려줘", plan)

    assert attached.requested_answer_shape.entities == (
        "E10",
        "E11",
        "E12",
        "E13",
        "E14",
    )


def test_q1_disease_name_expands_to_data_backed_kcd_set() -> None:
    expanded = expand_parameter_axes(
        _plan("당뇨병 환자수 알려줘"),
        "당뇨병 환자수 알려줘",
        observed_on=date(2026, 8, 14),
    )

    assert expanded.trace["axes"]["kcd_codes"] == ["E10", "E11", "E12", "E13", "E14"]
    assert expanded.plan.tool_queries.hira == tuple(
        f"{code} 환자수" for code in ("E10", "E11", "E12", "E13", "E14")
    )


def test_q1_hira_only_kcd_expansion_does_not_call_web() -> None:
    expanded = expand_parameter_axes(
        _plan("당뇨병 환자수 알려줘"),
        "당뇨병 환자수 알려줘",
        observed_on=date(2026, 8, 14),
    )

    assert expanded.plan.answer_sources == ("hira",)
    assert expanded.plan.tool_queries.web == ()


def test_q1_capped_kcd_plan_is_not_fanned_out_again_at_execution() -> None:
    expanded = expand_parameter_axes(
        _plan(
            "당뇨병 환자수 알려줘",
            entities=(
                "E10",
                "E11",
                "E12",
                "E13",
                "E14",
                "E10 (제1형 당뇨병)",
                "E11 (제2형 당뇨병)",
            ),
        ),
        "당뇨병 환자수 알려줘",
        observed_on=date(2026, 8, 14),
    )
    capped = apply_source_call_cap(expanded.plan)

    executable, _trace = _execution_plan(
        object(),
        capped,
        clinical_query_anchor="당뇨병 환자수 알려줘",
    )

    assert executable.tool_queries.hira == tuple(
        f"{code} 환자수" for code in ("E10", "E11", "E12", "E13", "E14")
    )


def test_q1_multi_year_cap_keeps_every_kcd_code_in_first_round() -> None:
    expanded = expand_parameter_axes(
        _plan(
            "당뇨병 환자수 알려줘",
            entities=("E10", "E11", "E12", "E13", "E14"),
        ).model_copy(
            update={"resolved_question": "당뇨병 E10 E11 E12 E13 E14 최근 5년 환자수"}
        ),
        "당뇨병 환자수 알려줘",
        observed_on=date(2026, 8, 14),
    )
    capped = apply_source_call_cap(expanded.plan)

    first_round = capped.tool_queries.hira[:5]

    assert tuple(query.split()[0] for query in first_round) == (
        "E10",
        "E11",
        "E12",
        "E13",
        "E14",
    )


def test_q1_multi_kcd_patient_gap_does_not_trigger_web_fill() -> None:
    plan = _plan(
        "당뇨병 환자수 알려줘",
        entities=("E10", "E11", "E12", "E13", "E14"),
    )
    missing_future = SourceResult(
        source="hira",
        query="E10 환자수 2026년",
        status="ok",
        payload={
            "period_coverage": {
                "periods": [{"period": "2026", "status": "no_data"}],
            }
        },
    )

    assert _gap_fill_request(plan, (missing_future,)) is None


def test_q1_interpreted_context_drives_axis_only_followup() -> None:
    plan = _plan("연령별로 다시 알려줘", entities=("E10", "E11", "E12", "E13", "E14"))
    plan = plan.model_copy(
        update={
            "resolved_question": "앞선 질문의 당뇨병 E10 E11 E12 E13 E14 환자수를 연령별로 조회",
            "expanded_intents": ("E10~E14 연령별 환자수",),
        }
    )

    expanded = expand_parameter_axes(plan, "연령별로 다시 알려줘", observed_on=date(2026, 8, 14))

    assert expanded.trace["axes"]["kcd_codes"] == ["E10", "E11", "E12", "E13", "E14"]
    assert all("연령별" in query for query in expanded.plan.tool_queries.hira)


def test_q5_axis_only_followup_inherits_full_entity_set_and_period() -> None:
    state = SessionState(
        canonical_entities=("E10", "E11", "E12", "E13", "E14"),
        primary_entity="E10",
        referenced_entity_set=("E10", "E11", "E12", "E13", "E14"),
        record_type="patient_count",
        time_window=("2022", "2023", "2024", "2025"),
    )

    bound = _bind_session_state_contract(_plan("연령별로 다시 알려줘"), "연령별로 다시 알려줘", state)

    assert all(code in bound.resolved_question for code in state.canonical_entities)
    assert all(year in bound.resolved_question for year in state.time_window)
    assert _session_inheritance_notice("연령별로 다시 알려줘", state) == (
        "앞선 질문의 E10 · E11 · E12 · E13 · E14 · 2022년 · 2023년 · "
        "2024년 · 2025년 기준으로 연령별을 조회했습니다."
    )


def test_q5_axis_only_patient_followup_keeps_only_hira_source() -> None:
    state = SessionState(
        canonical_entities=("E10", "E11", "E12", "E13", "E14"),
        primary_entity="E10",
        referenced_entity_set=("E10", "E11", "E12", "E13", "E14"),
        record_type="patient_count",
        time_window=("2022", "2023", "2024", "2025"),
    )

    bound = _bind_session_state_contract(
        _plan("연령별로 다시 알려줘"),
        "연령별로 다시 알려줘",
        state,
    )

    assert bound.answer_sources == ("hira",)
    assert bound.tool_queries.hira
    assert all(
        not queries
        for source, queries in bound.tool_queries.items()
        if source != "hira"
    )


def test_q4_gender_age_rows_keep_both_axis_labels_in_prompt_facts() -> None:
    payload = {
        "calls": [
            {
                "render_data": {
                    "request": {"sickCd": "D50", "year": "2023"},
                    "items": [
                        {
                            "sex": "남",
                            "age": "10~19세",
                            "sickCd": "D50",
                            "sickNm": "철결핍빈혈",
                            "ptntCnt": "1601",
                        }
                    ],
                }
            }
        ]
    }

    facts = _hira_patient_facts(payload)

    assert "남 · 10~19세 환자수는 1,601명" in facts[0]
    assert "환자 환자수" not in facts[0]


def test_q4_gender_age_surface_repair_rejects_unlabeled_patient_values() -> None:
    result = SourceResult(
        source="hira",
        query="D50 성별 연령별 환자수 2023년",
        status="ok",
        payload={
            "calls": [
                {
                    "render_data": {
                        "request": {"sickCd": "D50", "year": "2023"},
                        "items": [
                            {
                                "sex": "남",
                                "age": "10~19세",
                                "ptntCnt": "1601",
                                "units": {"ptntCnt": "명"},
                            }
                        ],
                    }
                }
            ]
        },
    )

    gated = apply_v4_gates(
        "23년 상병코드 D50의 성별/연령 10세 구간별 환자수를 비교해줘",
        "2023년 D50 환자 환자수 1,601명으로 확인되었습니다.",
        (result,),
    )

    assert "남 · 10~19세 환자수 1,601명" in gated.text
    assert "환자 환자수" not in gated.text
    assert gated.trace["requested_hira_surface"]["missing_after_repair"] == []


def test_q1_session_state_remembers_planned_kcd_codes_and_interpreted_period() -> None:
    plan = _plan(
        "당뇨병 환자수 알려줘",
        entities=("E10", "E11", "E12", "E13", "E14"),
    ).model_copy(
        update={"resolved_question": "당뇨병 E10 E11 E12 E13 E14 최근 5년 환자수"}
    )

    state = _derive_session_state(
        "당뇨병 환자수 알려줘",
        plan,
        (),
        previous=None,
    )

    assert state.canonical_entities == ("E10", "E11", "E12", "E13", "E14")
    assert state.referenced_entity_set == ("E10", "E11", "E12", "E13", "E14")
    assert state.time_window == ("recent_5y",)


def test_q1_disease_patent_expands_brand_set_without_overwrite() -> None:
    plan = _plan(
        "혈우병 치료제 특허 현황",
        entities=("기존엔티티",),
        answer_sources=("patent",),
    )

    expanded = expand_parameter_axes(plan, "혈우병 치료제 특허 현황", observed_on=date(2026, 8, 14))

    assert len(expanded.plan.tool_queries.patent) >= 2
    assert "기존엔티티" in expanded.plan.requested_answer_shape.entities
    assert len(expanded.trace["entity_expansion"]["entities"]) >= 2
    assert expanded.trace["entity_expansion"]["source"] == "query_expansion_data"


def test_q2_first_wave_limit_scales_with_entities_and_caps_queries(monkeypatch) -> None:
    monkeypatch.delenv("CHAT_V4_MAX_SOURCE_QUERIES", raising=False)
    monkeypatch.delenv("CHAT_V4_ENTITY_LIMIT", raising=False)
    entities = tuple(f"B{i}" for i in range(20))
    queries = tuple(f"B{i} 특허현황" for i in range(20))
    plan = _plan("브랜드 특허 비교", entities=entities, answer_sources=("patent",))
    plan = plan.model_copy(
        update={"tool_queries": plan.tool_queries.model_copy(update={"patent": queries})}
    )

    limited = planner._limit_first_wave_queries(plan)

    assert DEFAULT_ENTITY_LIMIT == 8
    assert HARD_ENTITY_LIMIT == 12
    assert SOURCE_CALL_LIMIT == 12
    assert configured_entity_limit() == DEFAULT_ENTITY_LIMIT
    assert len(limited.tool_queries.patent) == DEFAULT_ENTITY_LIMIT
    assert limited.query_scope is not None
    assert limited.query_scope.requested_calls["patent"] == 20
    assert limited.query_scope.executed_calls["patent"] == DEFAULT_ENTITY_LIMIT


def test_q2_entity_limit_can_increase_but_never_exceed_hard_cap(monkeypatch) -> None:
    monkeypatch.setenv("CHAT_V4_ENTITY_LIMIT", "99")
    entities = tuple(f"B{i}" for i in range(20))
    queries = tuple(f"B{i} 특허현황" for i in range(20))
    plan = _plan("브랜드 특허 비교", entities=entities, answer_sources=("patent",))
    plan = plan.model_copy(
        update={"tool_queries": plan.tool_queries.model_copy(update={"patent": queries})}
    )

    limited = planner._limit_first_wave_queries(plan)

    assert configured_entity_limit() == HARD_ENTITY_LIMIT
    assert len(limited.tool_queries.patent) == HARD_ENTITY_LIMIT


def test_q2_post_expansion_cap_is_visible_to_users_and_trace() -> None:
    queries = tuple(f"브랜드{i} 특허현황" for i in range(25))
    plan = _plan("25개 브랜드 특허 비교", answer_sources=("patent",))
    plan = plan.model_copy(
        update={"tool_queries": plan.tool_queries.model_copy(update={"patent": queries})}
    )

    limited = apply_source_call_cap(plan)

    assert len(limited.tool_queries.patent) == 12
    assert limited.query_scope is not None
    assert limited.query_scope.omitted_queries["patent"] == queries[12:]
    assert _query_scope_notice(limited) == (
        "특허 조회는 요청 25건 중 12건을 실행했습니다. "
        "나머지 13건은 이번 답변의 조회 상한으로 제외했습니다.\n"
        "제외 질의: 브랜드12 특허현황 · 브랜드13 특허현황 · 브랜드14 특허현황 · "
        "브랜드15 특허현황 · 브랜드16 특허현황 · 외 8건"
    )


def test_q2_post_expansion_cap_preserves_first_wave_omissions() -> None:
    queries = tuple(f"브랜드{i} 특허현황" for i in range(20))
    plan = _plan(
        "20개 브랜드 특허 비교",
        entities=tuple(f"브랜드{i}" for i in range(20)),
        answer_sources=("patent",),
    )
    plan = plan.model_copy(
        update={"tool_queries": plan.tool_queries.model_copy(update={"patent": queries})}
    )

    first_wave = planner._limit_first_wave_queries(plan)
    post_expansion = apply_source_call_cap(first_wave)

    assert len(post_expansion.tool_queries.patent) == DEFAULT_ENTITY_LIMIT
    assert post_expansion.query_scope is not None
    assert post_expansion.query_scope.requested_calls["patent"] == 20
    assert post_expansion.query_scope.executed_calls["patent"] == DEFAULT_ENTITY_LIMIT
    assert post_expansion.query_scope.omitted_queries["patent"] == queries[DEFAULT_ENTITY_LIMIT:]


def test_q2_final_execution_fanout_cannot_bypass_source_cap() -> None:
    entities = tuple(f"B{i}" for i in range(20))
    plan = _plan("브랜드 특허 비교", entities=entities, answer_sources=("patent",))

    executable, _trace = _execution_plan(
        object(),
        plan,
        clinical_query_anchor="브랜드 특허 비교",
    )

    assert len(executable.tool_queries.patent) == SOURCE_CALL_LIMIT
    assert executable.query_scope is not None
    assert executable.query_scope.requested_calls["patent"] == 20
    assert len(executable.query_scope.omitted_queries["patent"]) == 8


def test_q4_reimbursement_total_is_patient_statistics_not_criteria() -> None:
    query = "24년 상병코드 D693의 요양기관종별 요양급여비용총액을 알려줘"

    assert adapters._hira_query_kind(query) == "patient"


def test_q4_multiple_hira_axes_are_preserved() -> None:
    routes = adapters._hira_stat_routes(
        "23년 상병코드 D50의 성별 연령 10세 구간별 입원 외래 환자수를 비교해줘"
    )

    assert [route.tool for route in routes] == [
        "hira_disease_hospitalization_outpatient_stats",
        "hira_disease_gender_age_stats",
    ]
    assert [route.label for route in routes] == ["입원/외래", "성별·연령10세구간별"]


def test_q4_age_label_is_displayed_as_range_without_changing_raw_value() -> None:
    assert adapters._hira_age_label("10_19세") == "10~19세"
    assert adapters._hira_age_label("80세 이상") == "80세 이상"


def test_q7_failure_classifier_uses_http_status_and_body() -> None:
    assert classify_upstream_failure(http_status=429, body="") == "RATE_LIMITED"
    assert classify_upstream_failure(
        http_status=200,
        body="LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR",
    ) == "QUOTA_EXCEEDED"
    assert classify_upstream_failure(
        http_status=200,
        body="SERVICE_KEY_IS_NOT_REGISTERED_ERROR",
    ) == "AUTH_FAILED"
    assert classify_upstream_failure(http_status=503, body="unavailable") == "UPSTREAM_5XX"


def test_q7_failure_reason_survives_retrieval_event_and_public_notice() -> None:
    result = SourceResult(
        source="web",
        query="스타틴 안전성",
        status="quota",
        failure_reason="QUOTA_EXCEEDED",
        failure_detail={"http_status": 200},
    )

    event = retrieval_event_from_result(result)

    assert event.reason_code == "QUOTA_EXCEEDED"
    assert public_retrieval_notice(event, label="웹 검색") == (
        "웹 검색 사용량 한도 초과로 외부 조회가 실패해 확인할 수 없습니다."
    )


def test_q7_auth_and_5xx_have_distinct_user_messages() -> None:
    auth = retrieval_event_from_result(
        SourceResult(
            source="nedrug",
            query="리바로 효능",
            status="upstream",
            failure_reason="AUTH_FAILED",
        )
    )
    server = retrieval_event_from_result(
        SourceResult(
            source="hira",
            query="D693 환자수",
            status="upstream",
            failure_reason="UPSTREAM_5XX",
        )
    )

    assert "인증" in public_retrieval_notice(auth, label="의약품 정보")
    assert "상류 서비스 오류" in public_retrieval_notice(server, label="HIRA")


def test_q7_auth_failure_cannot_be_reported_as_zero_records() -> None:
    event = retrieval_event_from_result(
        SourceResult(
            source="nedrug",
            query="리바로 효능",
            status="empty",
            failure_reason="AUTH_FAILED",
        )
    )

    assert event.status == "upstream"
    assert "인증" in public_retrieval_notice(event, label="의약품 정보")
    assert "0건" not in public_retrieval_notice(event, label="의약품 정보")


def test_q7_failure_body_keeps_reason_but_masks_credentials() -> None:
    body = "SERVICE_KEY_IS_NOT_REGISTERED_ERROR serviceKey=secret-value"

    redacted = redact_failure_body(body)

    assert "SERVICE_KEY_IS_NOT_REGISTERED_ERROR" in redacted
    assert "secret-value" not in redacted
    assert "serviceKey=***" in redacted


def test_q6_monthly_mart_period_is_structured_and_dynamic(monkeypatch) -> None:
    monkeypatch.setattr(planner, "_current_kst_date", lambda: date(2026, 8, 14))

    shape = planner._requested_answer_shape("리바로 2026년 월별 매출")

    assert shape.granularity == "month"
    assert (shape.period_from, shape.period_to) == ("2026-01", "2026-08")
    assert adapters._month_span(shape.period_from, shape.period_to) == 8
