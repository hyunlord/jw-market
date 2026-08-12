from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from jw_chat_agent_poc.service.v4 import runtime as v4_runtime
from jw_chat_agent_poc.service.v4.claim_ir import classify_answer_claims
from jw_chat_agent_poc.service.v4.clinical import compile_clinical_query
from jw_chat_agent_poc.service.v4.clinical_query_policy import (
    query_entity_candidates,
    resolver_first_clinical_concepts,
)
from jw_chat_agent_poc.service.v4.planner import _requested_answer_shape
from jw_chat_agent_poc.service.v4.contracts import (
    ClinicalTrialConcept,
    PlannerOutput,
    RequestedAnswerShape,
    SourceResult,
    ToolQueries,
)
from jw_chat_agent_poc.service.v4.lossless_contracts import (
    CoverageLedger,
    EvidenceRecord,
    EvidenceSet,
)
from jw_chat_agent_poc.service.v4.render_clinical import render_clinical
from jw_chat_agent_poc.service.v4.retrieval_events import (
    RetrievalEvent,
    classify_failure_signals,
    public_retrieval_notice,
    retrieval_event_from_result,
)
from jw_chat_agent_poc.service.v4.executor import ParallelSourceExecutor
from jw_chat_agent_poc.service.v4.runtime import (
    V4Runtime,
    _tag_absence_context,
    _tag_gap_result,
)
from jw_chat_agent_poc.service.v4.source_tiers import (
    entity_completion_rows,
    fan_out_tier_zero_queries,
    source_tier,
    tier_funnel,
)
from jw_chat_agent_poc.service.v4.surface_binding import sanitize_bound_surface
from jw_chat_agent_poc.service.v4.synthesizer import SynthesisOutcome


def _plan(
    question: str,
    *,
    answer_sources: tuple[str, ...] = ("clinicaltrials",),
    entities: tuple[str, ...] = (),
) -> PlannerOutput:
    query_map = {
        source: (question,)
        for source in (
            "mart",
            "nedrug",
            "hira",
            "openfda",
            "clinicaltrials",
            "web",
            "patent",
        )
    }
    return PlannerOutput(
        resolved_question=question,
        expanded_intents=(question,),
        answer_sources=answer_sources,
        tool_queries=ToolQueries(**query_map),
        linking_plan="first hop is sufficient",
        requested_answer_shape=RequestedAnswerShape(entities=entities),
    )


def _clinical_set(*records: EvidenceRecord) -> EvidenceSet:
    return EvidenceSet(
        source="clinicaltrials",
        retrieved_at="2026-08-13T00:00:00Z",
        coverage=CoverageLedger(
            total_reported=len(records),
            records_received=len(records),
            records_unique=len(records),
        ),
        records=records,
    )


def _clinical_record(nct_id: str, *, sponsor: str = "JW중외제약") -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=f"ct:{nct_id}",
        source="clinicaltrials",
        result_kind="structured_clinical_record",
        payload={
            "nct_id": nct_id,
            "official_title": "Pitavastatin and ezetimibe study",
            "brief_title": "LivaloZet study",
            "phases": ["PHASE3"],
            "overall_status": "COMPLETED",
            "interventions": ["pitavastatin", "ezetimibe"],
            "sponsor": sponsor,
            "start_date": "2023-01-02",
            "url": f"https://clinicaltrials.gov/study/{nct_id}",
        },
    )


def test_a_unbound_entities_are_removed_from_every_prose_section() -> None:
    evidence = _clinical_set(_clinical_record("NCT05151731"))
    answer = (
        "## 핵심 답\nNCT05151731의 시험 설계를 확인했습니다.\n\n"
        "## 미확인 요소\n"
        "- 질의에 포함된 DS-7300a 및 Ifinatamab deruxtecan 관련 정보는 확인하지 못했습니다.\n"
        "- 선정 및 제외 기준은 원문 200자 제한으로 일부만 확인했습니다."
    )

    sanitized, trace = sanitize_bound_surface(
        "NCT05151731 시험 디자인",
        answer,
        (evidence,),
        (),
    )

    assert "DS-7300a" not in sanitized
    assert "Ifinatamab deruxtecan" not in sanitized
    assert "NCT05151731" in sanitized
    assert "선정 및 제외 기준" in sanitized
    assert trace["removed_unbound_lines"] == 1


def test_a_unbound_entities_are_removed_from_headings_and_tables() -> None:
    evidence = _clinical_set(_clinical_record("NCT05151731"))
    answer = (
        "## DS-7300a 확인 결과\n"
        "| 시험 | 상태 |\n"
        "| --- | --- |\n"
        "| Ifinatamab deruxtecan | 미확인 |\n"
        "| NCT05151731 | 완료 |"
    )

    sanitized, trace = sanitize_bound_surface(
        "NCT05151731 시험 디자인",
        answer,
        (evidence,),
        (),
    )

    assert "DS-7300a" not in sanitized
    assert "Ifinatamab deruxtecan" not in sanitized
    assert "NCT05151731" in sanitized
    assert "| --- | --- |" in sanitized
    assert trace["removed_unbound_lines"] == 2


def test_b_single_record_does_not_render_zero_information_aggregates() -> None:
    evidence = _clinical_set(_clinical_record("NCT05151731"))

    nodes, _ = render_clinical(evidence, single=True)
    block_ids = {node.block_id for node in nodes}

    assert "clinical:phase-status" not in block_ids
    assert "clinical:sponsor-groups" not in block_ids
    assert "clinical:records" in block_ids
    assert {
        record_id for node in nodes for record_id in node.record_ids
    } == {"ct:NCT05151731"}


def test_c_claim_ir_shadow_classifies_without_changing_answer() -> None:
    evidence = _clinical_set(
        _clinical_record("NCT05151731"),
        _clinical_record("NCT07470125", sponsor="Yuhan"),
    )
    answer = (
        "NCT05151731은 2023-01-02에 시작했습니다. "
        "NCT05151731과 NCT07470125는 서로 다른 시험입니다. "
        "두 시험이 시장 성장을 일으켰습니다."
    )

    classified = classify_answer_claims(answer, (evidence,))

    assert classified.answer == answer
    assert classified.answer_mutation is False
    assert [claim.claim_type for claim in classified.claim_ir] == ["T1", "T2", "T3"]
    assert classified.claim_ir[1].operator_id == "set_distinct"
    assert classified.recomputation_evidence


def test_d_retrieval_four_states_never_turn_failures_into_absence() -> None:
    cases = (
        ("empty", None, "이번 조회 조건에 맞는 레코드 0건"),
        ("timeout", "read timed out", "조회가 완료되지 않아 확인할 수 없습니다"),
        ("quota", "usage limit exceeded", "외부 조회가 실패해 확인할 수 없습니다"),
        ("upstream", "HTTP 503", "외부 조회가 실패해 확인할 수 없습니다"),
        ("parse_error", "malformed response", "검증 가능한 레코드로 변환하지 못했습니다"),
    )

    for status, notice, expected in cases:
        result = SourceResult(
            source="web",
            query="synthetic query",
            status=status,
            notice=notice,
        )
        event = retrieval_event_from_result(result, entity_id="synthetic")
        surface = public_retrieval_notice(event)
        assert expected in surface
        if status != "empty":
            assert "0건" not in surface
            assert "조회 결과가 없습니다" not in surface
    assert classify_failure_signals(("unsupported",), "") == "upstream"


def test_d_retrieval_event_has_stable_binding_record() -> None:
    result = SourceResult(
        source="patent",
        query="리바로젯 특허현황",
        status="timeout",
        elapsed_ms=6100,
        notice="정답 근거 도착 후 soft deadline으로 미포함",
    )

    first = retrieval_event_from_result(result, entity_id="리바로젯")
    second = retrieval_event_from_result(result, entity_id="리바로젯")

    assert isinstance(first, RetrievalEvent)
    assert first.record_id == second.record_id
    assert first.record_id.startswith("EXEC-")
    assert first.reason_code == "timeout"


def test_d_supplemental_tagging_never_turns_failure_into_empty() -> None:
    absence_request = {
        "source": "hira",
        "document": "reimbursement",
        "subject": "Mounjaro",
    }
    gap_request = {
        "source": "web",
        "query": "synthetic",
        "missing_periods": ("2025",),
    }

    for status in ("timeout", "quota", "upstream", "parse_error"):
        result = SourceResult(
            source="web",
            query="synthetic",
            status=status,
            payload={"items": []},
        )
        assert _tag_absence_context(result, absence_request).status == status
        assert _tag_gap_result(result, gap_request).status == status


def test_d_executor_adapter_exception_is_typed_as_upstream() -> None:
    def fail(_query: str) -> SourceResult:
        raise RuntimeError("synthetic upstream failure")

    def ok(query: str) -> SourceResult:
        return SourceResult(source="web", query=query, status="ok")

    adapters = {source: ok for source in ("mart", "nedrug", "hira", "openfda", "clinicaltrials", "web", "patent")}
    adapters["mart"] = fail
    outcome = ParallelSourceExecutor(adapters=adapters).execute_with_trace(
        _plan("리바로 매출", answer_sources=("mart",)),
        session_id="r12-2-exception",
        source_filter=("mart",),
    )

    assert outcome.results[0].status == "upstream"
    assert outcome.trace["tools"][0]["exclusion_reason"] == "upstream_error"


def test_e_source_tiers_preserve_primary_then_auxiliary_order() -> None:
    plan = _plan("리바로젯 급여기준", answer_sources=("hira",))

    assert source_tier(plan, "hira") == 0
    assert source_tier(plan, "openfda") == 1
    assert source_tier(plan, "web") == 2

    funnel = tier_funnel(
        plan,
        (
            SourceResult(source="hira", query="q", status="ok", payload={"rows": [1]}),
            SourceResult(source="openfda", query="q", status="ok", payload={"rows": [1, 2]}),
            SourceResult(source="web", query="q", status="timeout"),
        ),
        (),
        (),
    )
    assert funnel["tier_0"]["S2_results"] == 1
    assert funnel["tier_1"]["S2_results"] == 1
    assert funnel["tier_2"]["S2_results"] == 0


def test_f_partial_entity_rows_are_explicit_and_scope_comparison() -> None:
    plan = _plan(
        "리바로젯, 리피토, 리바로 매출 현황",
        answer_sources=("mart",),
        entities=("리바로젯", "리피토", "리바로"),
    )
    results = (
        SourceResult(source="mart", query="리바로젯 매출", status="ok", payload={"brand": "리바로젯"}),
        SourceResult(source="mart", query="리피토 매출", status="ok", payload={"brand": "리피토"}),
        SourceResult(source="mart", query="리바로 매출", status="timeout"),
    )

    coverage = entity_completion_rows(plan, results)

    assert [row["status"] for row in coverage.rows] == ["COMPLETE", "COMPLETE", "FAILED"]
    assert "확인된 2개 브랜드(리바로젯·리피토) 기준" in coverage.scope_notice
    assert "리바로" in coverage.missing_rows_markdown


def test_h_resolver_first_query_is_deterministic_and_contains_no_korean() -> None:
    resolution = SimpleNamespace(
        canonical_brand="리바로젯",
        molecule_en=("ezetimibe", "pitavastatin"),
    )
    planner = ClinicalTrialConcept(
        brands=("리바로젯 제네릭",),
        search_area="intervention",
        source_queries=("리바로젯 제네릭 임상현황",),
    )

    first = resolver_first_clinical_concepts(
        "리바로젯 제네릭 임상현황",
        resolution,
        planner,
    )
    second = resolver_first_clinical_concepts(
        "리바로젯 제네릭 임상현황",
        resolution,
        planner,
    )
    parameters = [compile_clinical_query(concept).parameters for concept in first.concepts]

    assert first == second
    assert first.blocked_reason is None
    assert parameters
    assert all(not any("가" <= char <= "힣" for char in str(item)) for item in parameters)
    assert parameters[0]["query.intr"] == "ezetimibe OR pitavastatin"


def test_h_resolver_first_preserves_planner_filters_for_every_entity() -> None:
    planner = ClinicalTrialConcept(
        countries=("Korea",),
        statuses=("RECRUITING",),
        source_queries=("clinical trials",),
    )
    resolutions = (
        SimpleNamespace(canonical_brand="리바로젯", molecule_en=("pitavastatin", "ezetimibe")),
        SimpleNamespace(canonical_brand="리피토", molecule_en=("atorvastatin",)),
    )

    parameters = [
        compile_clinical_query(
            resolver_first_clinical_concepts("임상 현황", resolution, planner).concepts[0]
        ).parameters
        for resolution in resolutions
    ]

    assert all(item["query.locn"] == "Korea" for item in parameters)
    assert all(item["filter.overallStatus"] == "RECRUITING" for item in parameters)


def test_h_known_brand_ignores_nondeterministic_planner_search_terms() -> None:
    resolution = SimpleNamespace(
        canonical_brand="리바로젯",
        molecule_en=("pitavastatin", "ezetimibe"),
    )
    first_planner = ClinicalTrialConcept(
        ingredients=("LivaloZet",),
        source_queries=("LivaloZet generic",),
    )
    second_planner = ClinicalTrialConcept(
        ingredients=("Pitavastatin Ezetimibe",),
        source_queries=("Pitavastatin Ezetimibe",),
    )

    first = resolver_first_clinical_concepts(
        "리바로젯 제네릭 임상현황",
        resolution,
        first_planner,
    )
    second = resolver_first_clinical_concepts(
        "리바로젯 제네릭 임상현황",
        resolution,
        second_planner,
    )

    assert [compile_clinical_query(item).parameters for item in first.concepts] == [
        compile_clinical_query(item).parameters for item in second.concepts
    ]
    assert len(first.concepts) == 1


def test_h_unresolved_korean_query_fails_typed_instead_of_silent_empty() -> None:
    planner = ClinicalTrialConcept(
        brands=("알수없는브랜드",),
        source_queries=("알수없는브랜드 임상",),
    )

    decision = resolver_first_clinical_concepts("알수없는브랜드 임상", None, planner)

    assert decision.concepts == ()
    assert decision.blocked_reason == "unresolved_korean_clinical_query"


def test_f_and_h_explicit_multi_entity_list_is_deterministic() -> None:
    question = "리바로젯, 리피토, 리바로 매출 현황"

    assert _requested_answer_shape(question).entities == (
        "리바로젯",
        "리피토",
        "리바로",
    )
    assert query_entity_candidates(question) == ("리바로젯", "리피토", "리바로")
    conjunction_question = "리바로젯과 리피토 매출 현황"
    assert _requested_answer_shape(conjunction_question).entities == ("리바로젯", "리피토")
    assert query_entity_candidates(conjunction_question) == ("리바로젯", "리피토")


def test_f_tier_zero_query_is_fanned_out_once_per_entity() -> None:
    plan = _plan(
        "리바로젯, 리피토, 리바로 매출 현황",
        answer_sources=("mart",),
        entities=("리바로젯", "리피토", "리바로"),
    )

    expanded = fan_out_tier_zero_queries(plan)

    assert expanded.tool_queries.mart == (
        "리바로젯 매출 현황",
        "리피토 매출 현황",
        "리바로 매출 현황",
    )
    assert expanded.tool_queries.hira == plan.tool_queries.hira
    assert fan_out_tier_zero_queries(expanded) == expanded


def test_c_runtime_shadow_trace_is_byte_identical_on_and_off(monkeypatch) -> None:
    plan = _plan("NCT05151731 시험 디자인")
    clinical = SourceResult(
        source="clinicaltrials",
        query="NCT05151731",
        status="ok",
        payload={
            "calls": [
                {
                    "tool": "clinicaltrials_search",
                    "status": "ok",
                    "render_data": {
                        "payload": {
                            "studies": [_clinical_record("NCT05151731").payload],
                            "totalCount": 1,
                        },
                        "query_manifest": {
                            "query_id": "ctq:one",
                            "compiled_expression": "NCT05151731",
                        },
                        "coverage": {
                            "total_reported": 1,
                            "records_received": 1,
                            "pagination_complete": True,
                        },
                    },
                }
            ]
        },
    )
    timeout = SourceResult(
        source="web",
        query="NCT05151731",
        status="timeout",
        notice="read timed out",
    )

    class Planner:
        def plan_with_trace(self, _question, _turns, *, budget_s):
            return SimpleNamespace(plan=plan, trace={"elapsed_ms": 1.0, "usage": {}})

        def link(self, *_args, **_kwargs):
            return None

    class Executor:
        def execute_with_trace(self, _plan, **_kwargs):
            return SimpleNamespace(results=(clinical, timeout), trace={"elapsed_ms": 1.0, "tools": []})

    class Synthesizer:
        def synthesize_with_trace(
            self,
            _plan,
            _results,
            _turns,
            *,
            budget_s,
            deterministic_facts,
        ):
            return SynthesisOutcome(
                text="## 핵심 답\nNCT05151731의 시험 설계를 확인했습니다.",
                trace={"elapsed_ms": 1.0, "usage": {}},
            )

    monkeypatch.setenv("CHAT_V4_LOSSLESS_SPINE_MODE", "inject")
    monkeypatch.setenv("CHAT_CLAIM_IR_SHADOW", "true")
    enabled = V4Runtime(
        planner=Planner(), executor=Executor(), synthesizer=Synthesizer()
    ).answer(plan.resolved_question, conversation_id="r12-2-shadow-on", turns=())
    monkeypatch.setenv("CHAT_CLAIM_IR_SHADOW", "false")
    disabled = V4Runtime(
        planner=Planner(), executor=Executor(), synthesizer=Synthesizer()
    ).answer(plan.resolved_question, conversation_id="r12-2-shadow-off", turns=())

    assert enabled.text == disabled.text
    assert enabled.trace["claim_ir_shadow"]["answer_mutation"] is False
    assert enabled.trace["claim_ir_shadow"]["input_answer_sha256"] == enabled.trace["claim_ir_shadow"]["output_answer_sha256"]
    assert enabled.trace["claim_ir_shadow"]["claim_ir"]
    assert disabled.trace["claim_ir_shadow"]["status"] == "disabled"
    assert enabled.trace["retrieval_events"][1]["reason_code"] == "timeout"
    assert enabled.trace["retrieval_events"][1]["deadline_at"] is not None
    assert enabled.trace["source_tier_funnel"]["tier_0"]["S3_records"] == 1

    original_classifier = v4_runtime.classify_answer_claims

    def mutating_classifier(answer, evidence_sets):
        classified = original_classifier(answer, evidence_sets)
        return classified.model_copy(
            update={"answer": "MUTATED", "answer_mutation": True}
        )

    monkeypatch.setattr(v4_runtime, "classify_answer_claims", mutating_classifier)
    monkeypatch.setenv("CHAT_CLAIM_IR_SHADOW", "true")
    protected = V4Runtime(
        planner=Planner(), executor=Executor(), synthesizer=Synthesizer()
    ).answer(plan.resolved_question, conversation_id="r12-2-shadow-protected", turns=())

    assert protected.text == enabled.text
    assert protected.trace["claim_ir_shadow"]["status"] == "contract_violation"
    assert protected.trace["claim_ir_shadow"]["answer_mutation"] is False
