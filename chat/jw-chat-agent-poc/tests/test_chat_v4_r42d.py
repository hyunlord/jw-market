from __future__ import annotations

from datetime import date

from jw_chat_agent_poc.service import app as service_app
from jw_chat_agent_poc.service.context_scope import ContextScope, resolve_context_scope
from jw_chat_agent_poc.service.file_sql_query import (
    METRIC_AMOUNT,
    MetricScope,
    SqlFileSource,
    _render_aggregate_answer,
)
from jw_chat_agent_poc.service.v4.inspection import (
    _inspection_output,
    _raw_records,
    build_inspection_detail,
)
from jw_chat_agent_poc.service.v4.deterministic_render import _render_set
from jw_chat_agent_poc.service.v4.contracts import RequestedAnswerShape, SourceResult
from jw_chat_agent_poc.service.v4.lossless_contracts import (
    CoverageLedger,
    DeterministicRender,
    EvidenceRecord,
    EvidenceSet,
    RenderNode,
)
from jw_chat_agent_poc.service.v4.lossless_spine import compose_lossless_answer
from jw_chat_agent_poc.service.v4.render_policy import render_policy
from jw_chat_agent_poc.service.v4.runtime import (
    _attach_entity_completion_to_inspection,
    _inject_entity_completion_surface,
    _layout_axis_question,
)
from jw_chat_agent_poc.service.v4.source_tiers import entity_completion_rows
from test_chat_v4_r42b import _plan, _rendered


def test_active_file_session_runs_file_and_market_as_peer_lanes() -> None:
    scope = resolve_context_scope(
        "제이더블유중외제약 제품 매출 상위 10개",
        has_active_file=True,
        has_market_intent=True,
        has_market_anchor=True,
        file_schema_columns=("PRODUCT NAME KOR", "VALUES LC SI PRICE 1/2026"),
    )

    assert scope is ContextScope.MIXED


def test_mixed_file_answer_leads_and_marks_other_source_as_non_substitute() -> None:
    final = service_app.compute_final_answer(
        "첨부 엑셀에서 제이더블유중외제약 제품 매출 상위 10개",
        {
            "context_scope": "MIXED",
            "mixed_market_question": "제이더블유중외제약 제품 매출 상위 10개",
            "mixed_market_result": {
                "general_view_ready": True,
                "answer": "시장 데이터에서 확인한 참고 결과입니다.",
                "sources": ["UBIST"],
                "tool_calls": [],
                "markdown_response": {},
            },
            "mixed_file_result": {
                "sources": ["document"],
                "tool_calls": [],
                "file_source_items": [{"file_name": "CHSO.xlsx"}],
                "mixed_leg_error": "첨부 파일을 실행했지만 요청 대상은 0건입니다.",
            },
        },
        "mixed-file-peer-lanes",
    )

    assert final.text.index("## 첨부 문서") < final.text.index("## 참고: 다른 출처")
    assert "요청 대상과 출처·분류·지표 정의가 다른 참고 자료" in final.text
    assert "요청 대상을 대체하지 않습니다" in final.text
    assert "대신 안내" not in final.text


def test_sell_in_file_aggregate_discloses_metric_and_converts_won_to_eok() -> None:
    answer = _render_aggregate_answer(
        "제이더블유중외제약 제품 매출 상위 10개",
        SqlFileSource("doc-91", "CHSO.xlsx", "Sell In Standard"),
        "SELECT c2, SUM(c72) AS total_value, COUNT(*) AS applied_rows FROM data GROUP BY c2",
        {
            "columns": ["c2", "total_value", "applied_rows"],
            "rows": [["제품A", 358_596_184_360, 10]],
        },
        {
            "columns": [
                {"query_name": "c2", "source_name": "PRODUCT NAME KOR"},
                {"query_name": "c72", "source_name": "VALUES LC SI PRICE 1/2026"},
            ]
        },
        metric=MetricScope(
            family=METRIC_AMOUNT,
            label="금액",
            defaulted=False,
            columns=(("2026-01", "c72"),),
        ),
    )

    assert "지표: sell-in 기준 금액" in answer
    assert "3,586억원" in answer
    assert "358,596,184,360" not in answer
    assert "원 단위" not in answer


def test_file_amount_below_one_eok_does_not_render_as_zero() -> None:
    answer = _render_aggregate_answer(
        "제품 매출",
        SqlFileSource("doc-small", "small.xlsx", "Sell In Standard"),
        "SELECT SUM(c2) AS total_value, COUNT(*) AS applied_rows FROM data",
        {
            "columns": ["total_value", "applied_rows"],
            "rows": [[49_000_000, 1]],
        },
        {
            "columns": [
                {"query_name": "c2", "source_name": "VALUES LC SI PRICE 1/2026"},
            ]
        },
        metric=MetricScope(
            family=METRIC_AMOUNT,
            label="금액",
            defaulted=False,
            columns=(("2026-01", "c2"),),
        ),
    )

    assert "0.49억원" in answer
    assert "0억원" not in answer


def test_primary_patient_facts_are_inside_single_nonempty_core_section() -> None:
    rendered = _rendered(
        RenderNode(
            block_id="hira-statistics:records",
            record_ids=("hira:E10:outpatient",),
            text=(
                "## 환자수·비용\n"
                "| 상병코드 | 구분 | 환자수 |\n"
                "| --- | --- | ---: |\n"
                "| E10 | 외래 | 55,228 |"
            ),
        )
    )

    composed = compose_lossless_answer(
        rendered,
        "**핵심 답**\n\n**근거와 맥락**\n참고 설명입니다.\n\n**종합 인사이트**",
        synthesis_trace={"status": "synthesized"},
        mode="inject",
        question="당뇨병 환자수 알려줘",
    )

    assert composed.text.count("## 핵심 답") == 1
    core = composed.text.split("## 핵심 답\n", 1)[1].split("\n## ", 1)[0]
    assert "55,228" in core
    assert "### 환자수·비용" in core
    assert "**핵심 답**" not in composed.text
    assert "종합 인사이트" not in composed.text


def test_hira_inspection_projects_actual_values_and_collapses_identical_display_rows() -> None:
    records = (
        {
            "sickCd": "E10",
            "sickNm": "1형 당뇨병",
            "inpatOpat": "외래",
            "ptntCnt": "55228",
            "rvdInsupBrdnAmt": "8849490000",
            "rvdRpeTamtAmt": "16172094000",
            "sex": None,
            "age": "0~9세",
            "sexBreakdown": [{"sex": "남", "ptntCnt": "30659"}],
            "year": "2025",
        },
        {
            "sickCd": "E10",
            "sickNm": "1형 당뇨병",
            "inpatOpat": "외래",
            "ptntCnt": "55228",
            "rvdInsupBrdnAmt": "8849490000",
            "rvdRpeTamtAmt": "16172094000",
            "sex": None,
            "age": "0~9세",
            "sexBreakdown": [{"sex": "남", "ptntCnt": "30659"}],
            "year": "2025",
        },
    )

    output, _metrics = _inspection_output(records, 2, source="hira")

    assert output["returned"] == 2
    assert output["displayed_record_count"] == 1
    assert output["duplicate_records_collapsed"] == 1
    assert output["records"][0]["duplicate_count"] == 2
    assert output["records"][0]["duplicate_label"] == "동일 항목 2건"
    assert output["records"][0]["ptntCnt"] == "55228"
    assert output["records"][0]["inpatOpat"] == "외래"
    assert output["records"][0]["rvdInsupBrdnAmt"] == "8849490000"
    assert output["records"][0]["year"] == "2025"
    assert output["records"][0]["age"] == "0~9세"
    assert output["records"][0]["sexBreakdown"] == [{"sex": "남", "ptntCnt": "30659"}]


def test_clinical_inspection_output_includes_public_id_and_title() -> None:
    output, _metrics = _inspection_output(
        (
            {
                "nct_id": "NCT07523971",
                "status": "RECRUITING",
                "phase": "PHASE3",
                "brief_title": "Pitavastatin and Ezetimibe Study",
            },
        ),
        1,
        source="clinicaltrials",
    )

    record = output["records"][0]
    assert record["identifiers"] == ["NCT07523971", "Pitavastatin and Ezetimibe Study"]
    assert record["nct_id"] == "NCT07523971"
    assert record["status"] == "RECRUITING"
    assert record["phase"] == "PHASE3"
    assert record["brief_title"] == "Pitavastatin and Ezetimibe Study"


def test_raw_records_accepts_list_shaped_call_render_data() -> None:
    payload = {
        "calls": [
            {
                "render_data": [
                    {"sickCd": "E10", "ptntCnt": "55228"},
                    {"sickCd": "E11", "ptntCnt": "3712401"},
                ]
            }
        ]
    }

    assert _raw_records(payload) == [
        {"sickCd": "E10", "ptntCnt": "55228"},
        {"sickCd": "E11", "ptntCnt": "3712401"},
    ]


def test_inspection_exposes_lane_groups_without_removing_calls() -> None:
    from jw_chat_agent_poc.service.v4.contracts import SourceResult

    results = (
        SourceResult(source="hira", query="E10 환자수", status="ok", payload={"rows": []}),
        SourceResult(source="hira", query="E11 환자수", status="ok", payload={"rows": []}),
    )
    detail = build_inspection_detail(
        _plan("당뇨병 환자수"),
        results,
        (),
        DeterministicRender(profile="market_analysis"),
    )

    assert len(detail["calls"]) == 2
    assert detail["lane_groups"] == [
        {
            "source_label": "건강보험심사평가원",
            "call_count": 2,
            "sequences": [1, 2],
            "returned": 0,
            "elapsed_seconds": 0.0,
        }
    ]


def test_policy_keeps_unmatched_notice_in_inspection_but_out_of_answer_body() -> None:
    records = (
        EvidenceRecord(
            evidence_id="hira:notice:matched",
            source="hira",
            result_kind="policy_document",
            payload={
                "notice_number": "제2021-245호",
                "source_date": "2021-10-01",
                "title": "보험인정기준 상세내용",
                "request": {"brand": "리바로젯"},
                "brand_name": "리바로젯",
                "matching_basis": "품명 '리바로젯정' 기준",
                "match_candidates": ["리바로젯정"],
                "raw_text": (
                    "■ 고시 개정 전체내용 허가사항 및 [일반원칙] 고지혈증 치료제 세부사항 범위 내에서 인정함. "
                    "■ 고시 개정 사유 리바로젯정 등재에 따라 성분 조합을 추가함 "
                    "■ 변경 전 고시번호 제2018-253호"
                ),
            },
        ),
        EvidenceRecord(
            evidence_id="hira:notice:ingredient-only",
            source="hira",
            result_kind="policy_document",
            payload={
                "notice_number": "제2026-92호",
                "source_date": "2026-05-01",
                "title": "고혈압치료제와 고지혈증 치료제 복합경구제",
                "request": {"brand": "리바로젯"},
                "brand_name": "리바로브이",
                "matching_basis": "성분 pitavastatin 기준",
                "match_candidates": ["리바로브이정"],
                "raw_text": "Valsartan + Pitavastatin 조합의 급여가 인정됨",
            },
        ),
        EvidenceRecord(
            evidence_id="hira:notice:unmatched",
            source="hira",
            result_kind="policy_document",
            payload={
                "notice_number": "제2026-138호",
                "source_date": "2026-07-01",
                "title": "의료급여 일반기준 일부개정",
                "request": {"brand": "리바로젯"},
                "brand_name": "보건복지부",
                "matching_basis": "",
                "match_candidates": [],
                "raw_text": "사회보장 전산관리번호 개편에 따른 규정 변경",
            },
        ),
    )
    evidence = EvidenceSet(
        source="hira",
        retrieved_at="2026-08-17T00:00:00+00:00",
        coverage=CoverageLedger(records_received=3, records_unique=3),
        records=records,
    )

    nodes, _required = render_policy(evidence, require_product_match=True)
    body = "\n\n".join(node.text for node in nodes)

    assert "제2021-245호" in body
    assert "제2026-92호" not in body
    assert "Valsartan + Pitavastatin" not in body
    assert "제2026-138호" not in body
    assert "사회보장 전산관리번호" not in body
    assert "세부 급여 인정 조건([일반원칙] 고지혈증 치료제)은 확인하지 못했습니다" in body
    assert len(body) < 1000
    assert {record.evidence_id for record in evidence.records} == {
        "hira:notice:matched",
        "hira:notice:ingredient-only",
        "hira:notice:unmatched",
    }


def test_policy_renders_no_fact_when_only_ingredient_overlap_exists() -> None:
    record = EvidenceRecord(
        evidence_id="hira:notice:ingredient-only",
        source="hira",
        result_kind="policy_document",
        payload={
            "notice_number": "제2026-92호",
            "request": {"brand": "리바로젯"},
            "brand_name": "리바로브이",
            "matching_basis": "성분 pitavastatin 기준",
            "match_candidates": ["리바로브이정"],
            "raw_text": "Valsartan + Pitavastatin 조합의 급여가 인정됨",
        },
    )
    evidence = EvidenceSet(
        source="hira",
        retrieved_at="2026-08-17T00:00:00+00:00",
        coverage=CoverageLedger(records_received=1, records_unique=1),
        records=(record,),
    )

    nodes, _required = render_policy(evidence, require_product_match=True)

    assert nodes == []
    assert evidence.records == (record,)


def test_policy_product_match_does_not_accept_brand_prefix_collision() -> None:
    record = EvidenceRecord(
        evidence_id="hira:notice:prefix-collision",
        source="hira",
        result_kind="policy_document",
        payload={
            "notice_number": "제2026-92호",
            "request": {"brand": "리바로"},
            "brand_name": "리바로젯정",
            "match_candidates": ["리바로젯정"],
            "raw_text": "리바로젯 대상 기준",
        },
    )
    evidence = EvidenceSet(
        source="hira",
        retrieved_at="2026-08-17T00:00:00+00:00",
        coverage=CoverageLedger(records_received=1, records_unique=1),
        records=(record,),
    )

    nodes, _required = render_policy(evidence, require_product_match=True)

    assert nodes == []


def test_policy_dispatch_always_requires_product_match() -> None:
    record = EvidenceRecord(
        evidence_id="hira:notice:wrong-product",
        source="hira",
        result_kind="policy_document",
        payload={
            "notice_number": "제2026-92호",
            "request": {"brand": "리바로젯"},
            "brand_name": "리바로브이",
            "match_candidates": ["리바로브이정"],
            "raw_text": "Valsartan + Pitavastatin 조합 기준",
        },
    )
    evidence = EvidenceSet(
        source="hira",
        retrieved_at="2026-08-17T00:00:00+00:00",
        coverage=CoverageLedger(records_received=1, records_unique=1),
        records=(record,),
    )

    nodes, _required = _render_set(
        "market_analysis",
        evidence,
        observed_on=date.today(),
        primary=True,
    )

    assert nodes == []


def test_reimbursement_moves_market_only_insight_to_reference_section() -> None:
    rendered = _rendered(
        RenderNode(
            block_id="policy:1:info",
            record_ids=("hira:notice:matched",),
            text="## 고시 정보\n| 항목 | 값 |\n| --- | --- |\n| 고시번호 | 제2021-245호 |",
        )
    )

    composed = compose_lossless_answer(
        rendered,
        "## 핵심 답\n급여기준을 확인했습니다.\n\n## 종합 인사이트\n매출은 증가했습니다.",
        synthesis_trace={"status": "synthesized"},
        mode="inject",
        question="리바로젯 급여기준 알려줘",
    )

    assert "## 종합 인사이트\n매출은" not in composed.text
    assert "## 참고: 인접 연구\n매출은 증가했습니다." in composed.text


def test_multiturn_layout_uses_current_turn_and_requested_measure_not_resolved_noise() -> None:
    plan = _plan("당뇨병 환자수를 성별 및 연령대별로 알려줘. 당뇨병 치료제 시장 현황")
    plan = plan.model_copy(
        update={
            "requested_answer_shape": RequestedAnswerShape(
                entities=("당뇨병", "E10", "성별", "연령대"),
                measure_or_attribute=("환자수",),
            )
        }
    )

    axis_question = _layout_axis_question("성별, 나이 기준으로도 알려줘", plan)

    assert "환자수" in axis_question
    assert "매출" not in axis_question
    assert "시장 현황" not in axis_question


def test_multiturn_layout_normalizes_canonical_patient_measure() -> None:
    plan = _plan("당뇨병 환자수를 성별 및 연령대별로 알려줘")
    plan = plan.model_copy(
        update={
            "requested_answer_shape": RequestedAnswerShape(
                entities=("당뇨병", "E10"),
                measure_or_attribute=("patient_count",),
            )
        }
    )

    axis_question = _layout_axis_question("성별, 나이 기준으로도 알려줘", plan)

    assert "환자수" in axis_question
    assert "patient_count" not in axis_question


def test_question_fragments_and_render_axes_are_not_completion_entities() -> None:
    plan = _plan("당뇨병 환자수를 성별 및 연령대별로 알려줘")
    plan = plan.model_copy(
        update={
            "answer_sources": ("hira",),
            "requested_answer_shape": RequestedAnswerShape(
                entities=(
                    "당뇨병",
                    "E10",
                    "성별",
                    "연령대",
                    "나이 기준으로도 알려줘",
                ),
                measure_or_attribute=("환자수",),
            ),
        }
    )
    results = (SourceResult(source="hira", query="E10 환자수", status="ok"),)

    completion = entity_completion_rows(plan, results)

    assert [row["entity"] for row in completion.rows] == ["당뇨병", "E10"]
    assert "나이 기준으로도 알려줘" not in completion.scope_notice


def test_entity_completion_labels_brand_disease_and_code_separately() -> None:
    plan = _plan("리바로젯 급여기준과 고지혈증 E78")
    plan = plan.model_copy(
        update={
            "answer_sources": ("hira",),
            "requested_answer_shape": RequestedAnswerShape(
                entities=("리바로젯", "고지혈증", "E78"),
                measure_or_attribute=("급여기준",),
            ),
        }
    )

    completion = entity_completion_rows(
        plan,
        (
            SourceResult(
                source="hira",
                query="리바로젯 급여기준",
                status="ok",
                payload={"records": [{"brand_name": "리바로젯"}]},
            ),
        ),
    )

    assert list(completion.entity_types) == [
        {"entity": "리바로젯", "entity_type": "브랜드"},
        {"entity": "고지혈증", "entity_type": "질환"},
        {"entity": "E78", "entity_type": "상병코드"},
    ]
    assert "상병코드·질환 항목(리바로젯" not in completion.scope_notice
    assert "조회 대상" in completion.scope_notice


def test_entity_collection_status_stays_in_trace_and_only_notice_reaches_body() -> None:
    plan = _plan("당뇨병 환자수")
    plan = plan.model_copy(
        update={
            "answer_sources": ("hira",),
            "requested_answer_shape": RequestedAnswerShape(entities=("E10", "E11")),
        }
    )
    completion = entity_completion_rows(
        plan,
        (SourceResult(source="hira", query="E10 환자수", status="ok"),),
    )

    answer, trace = _inject_entity_completion_surface("## 핵심 답\nE10 환자수입니다.", completion)

    assert "조회 대상별 수집 상태" not in answer
    assert "| 대상 | 상태 |" not in answer
    assert "E11" in answer
    assert trace["table_location"] == "inspection"
    assert trace["row_count"] == 2


def test_entity_collection_status_is_attached_to_inspection_detail() -> None:
    plan = _plan("당뇨병 환자수")
    plan = plan.model_copy(
        update={
            "answer_sources": ("hira",),
            "requested_answer_shape": RequestedAnswerShape(entities=("E10", "E11")),
        }
    )
    completion = entity_completion_rows(
        plan,
        (SourceResult(source="hira", query="E10 환자수", status="ok"),),
    )

    detail = _attach_entity_completion_to_inspection(
        {"schema": "r12.5.inspect.v1", "calls": []},
        completion,
    )

    assert detail["entity_completion"]["rows"] == list(completion.rows)
    assert detail["entity_completion"]["scope_notice"] == completion.scope_notice
    assert detail["entity_completion"]["table_location"] == "inspection"


def test_primary_absence_uses_correct_korean_topic_particle() -> None:
    rendered = _rendered(
        notices=("내부 데이터마트 조회는 실행되지 않았습니다.",),
        bindings=({"tool": "mart", "reason_code": "not_executed"},),
    )

    composed = compose_lossless_answer(
        rendered,
        "## 근거와 맥락\n참고 자료입니다.",
        synthesis_trace={"status": "synthesized"},
        mode="inject",
        question="리바로 매출 알려줘",
    )

    assert "요청하신 매출은 이번 조회에서 확인하지 못했습니다(실행 안 함)." in composed.text
    assert "매출는" not in composed.text


def test_patient_age_surface_keeps_one_core_and_reports_missing_female_axis() -> None:
    rows = "\n".join(
        (
            "| E10 | 1형 당뇨병 | 남 | 0~9세 | 350 |",
            "| E10 | 1형 당뇨병 | 남 | 10~19세 | 1,601 |",
            "| E11 | 2형 당뇨병 | 남 | 0~9세 | 111 |",
        )
    )
    rendered = _rendered(
        RenderNode(
            block_id="narrative:field-restatement",
            record_ids=("hira:E10:0", "hira:E10:10", "hira:E11:0"),
            text=(
                "2025년 E10 남 · 0~9세 환자수 350명으로 확인되었습니다.\n"
                "2025년 E10 남 · 10~19세 환자수 1,601명으로 확인되었습니다.\n"
                "2025년 E11 남 · 0~9세 환자수 111명으로 확인되었습니다."
            ),
        ),
        RenderNode(
            block_id="hira-statistics:records",
            record_ids=("hira:E10:0", "hira:E10:10", "hira:E11:0"),
            text=(
                "## 환자수·비용\n"
                "| 상병코드 | 상병명 | 성별 | 연령대 | 환자수 |\n"
                "| --- | --- | --- | --- | ---: |\n"
                f"{rows}"
            ),
        )
    )

    composed = compose_lossless_answer(
        rendered,
        (
            "## 핵심 답\n남성 연령대별 수치를 확인했습니다.\n\n"
            "## 핵심 답\n같은 수치를 다시 설명합니다.\n\n"
            "## 종합 인사이트\n근거 없이 전체 성별로 일반화합니다."
        ),
        synthesis_trace={"status": "synthesized"},
        mode="inject",
        question="성별, 나이 기준으로도 알려줘 환자수",
    )

    assert composed.text.count("## 핵심 답") == 1
    assert "여성 연령대별 자료는 이번 조회에서 확인하지 못했습니다" in composed.text
    assert "0~9세" in composed.text
    assert "09세" not in composed.text
    assert "환자수 350명으로 확인되었습니다" not in composed.text
    assert composed.trace["homogeneous_table_promotion_threshold"] == 3
    assert composed.trace["homogeneous_patient_narratives_promoted"] == 3
