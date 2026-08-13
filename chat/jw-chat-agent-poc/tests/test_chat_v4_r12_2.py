from __future__ import annotations

from datetime import date
import time
from types import SimpleNamespace

import pytest

from jw_chat_agent_poc.service.v4 import runtime as v4_runtime
from jw_chat_agent_poc.service.v4 import clinical_query_policy
from jw_chat_agent_poc.service.v4.claim_ir import classify_answer_claims
from jw_chat_agent_poc.service.v4.clinical import compile_clinical_query
from jw_chat_agent_poc.service.v4.clinical_query_policy import (
    prepare_resolved_clinical_requests,
    query_entity_candidates,
    resolver_first_clinical_concepts,
)
from jw_chat_agent_poc.service.v4.planner import (
    _attach_lossless_contracts,
    _requested_answer_shape,
)
from jw_chat_agent_poc.service.v4.contracts import (
    SOURCE_NAMES,
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
from jw_chat_agent_poc.service.v4.session_state import SessionState
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


def test_a_binding_removes_headings_left_empty_by_filtered_prose() -> None:
    evidence = _clinical_set(_clinical_record("NCT05151731"))
    answer = (
        "## 핵심 답\n"
        "DS-7300a 시험 설계를 확인했습니다.\n\n"
        "## 근거와 맥락\n"
        "NCT05151731의 시험 설계를 확인했습니다.\n\n"
        "### 참고: 인접 연구\n\n"
        "## 출처\n"
        "- ClinicalTrials.gov"
    )

    sanitized, trace = sanitize_bound_surface(
        "NCT05151731 시험 디자인",
        answer,
        (evidence,),
        (),
    )

    assert sanitized.startswith("## 핵심 답\nNCT05151731의 시험 설계를 확인했습니다.")
    assert "### 참고: 인접 연구" not in sanitized
    assert "## 근거와 맥락" not in sanitized
    assert "## 출처" in sanitized
    assert trace["removed_unbound_lines"] == 1
    assert trace["core_section_recovered_from"] == "근거와 맥락"


def test_a_binding_preserves_markdown_header_while_filtering_data_rows() -> None:
    evidence = _clinical_set(_clinical_record("NCT05151731"))
    answer = (
        "## 임상시험 전건\n"
        "| NCT ID | 단계 | 상태 |\n"
        "| --- | --- | --- |\n"
        "| NCT05151731 | PHASE2 | COMPLETED |\n"
        "| DS-7300a | PHASE1 | UNKNOWN |"
    )

    sanitized, trace = sanitize_bound_surface(
        "NCT05151731 시험 디자인",
        answer,
        (evidence,),
        (),
    )

    assert "| NCT ID | 단계 | 상태 |" in sanitized
    assert "| NCT05151731 | PHASE2 | COMPLETED |" in sanitized
    assert "DS-7300a" not in sanitized
    assert trace["removed_unbound_lines"] == 1


def test_a_binding_preserves_supported_english_schema_header() -> None:
    evidence = _clinical_set(_clinical_record("NCT05151731"))
    answer = (
        "## 임상시험 전건\n"
        "| NCT ID | Official Title | Overall Status |\n"
        "| --- | --- | --- |\n"
        "| NCT05151731 | Pitavastatin and ezetimibe study | COMPLETED |"
    )

    sanitized, trace = sanitize_bound_surface(
        "NCT05151731 시험 디자인",
        answer,
        (evidence,),
        (),
    )

    assert sanitized == answer
    assert trace["answer_mutation"] is False


def test_a_binding_removes_source_url_not_present_in_evidence() -> None:
    evidence = _clinical_set(_clinical_record("NCT05151731"))
    answer = (
        "## 핵심 답\nNCT05151731의 시험 설계를 확인했습니다.\n\n"
        "## 출처\n"
        "- [ClinicalTrials.gov](https://clinicaltrials.gov/study/NCT05151731)\n"
        "- [Unrelated](https://example.invalid/unrelated)"
    )

    sanitized, trace = sanitize_bound_surface(
        "NCT05151731 시험 디자인",
        answer,
        (evidence,),
        (),
    )

    assert "https://clinicaltrials.gov/study/NCT05151731" in sanitized
    assert "https://example.invalid/unrelated" not in sanitized
    assert trace["removed_unbound_source_lines"] == 1


def test_a_binding_requires_exact_evidence_url_not_a_prefix() -> None:
    evidence = _clinical_set(_clinical_record("NCT05151731"))
    answer = (
        "## 핵심 답\nNCT05151731의 시험 설계를 확인했습니다.\n\n"
        "## 출처\n"
        "- [Impostor](https://clinicaltrials.gov/study/NCT05151731.evil.invalid)"
    )

    sanitized, trace = sanitize_bound_surface(
        "NCT05151731 시험 디자인",
        answer,
        (evidence,),
        (),
    )

    assert "evil.invalid" not in sanitized
    assert trace["removed_unbound_source_lines"] == 1


def test_a_binding_does_not_promote_question_url_to_evidence() -> None:
    evidence = _clinical_set(_clinical_record("NCT05151731"))
    supplied_url = "https://example.invalid/user-supplied"
    answer = (
        "## 핵심 답\nNCT05151731의 시험 설계를 확인했습니다.\n\n"
        "## 출처\n"
        f"- [사용자 입력 링크]({supplied_url})"
    )

    sanitized, trace = sanitize_bound_surface(
        f"{supplied_url} NCT05151731 시험 디자인",
        answer,
        (evidence,),
        (),
    )

    assert supplied_url not in sanitized
    assert trace["removed_unbound_source_lines"] == 1


def test_a_binding_does_not_promote_url_echoed_in_query_payload() -> None:
    supplied_url = "https://example.invalid/user-supplied"
    evidence = _clinical_set(
        EvidenceRecord(
            evidence_id="ct:NCT05151731",
            source="clinicaltrials",
            result_kind="structured_clinical_record",
            payload={
                **_clinical_record("NCT05151731").payload,
                "query": supplied_url,
            },
        )
    )
    answer = (
        "## 핵심 답\nNCT05151731의 시험 설계를 확인했습니다.\n\n"
        "## 출처\n"
        f"- [사용자 입력 링크]({supplied_url})"
    )

    sanitized, trace = sanitize_bound_surface(
        f"{supplied_url} NCT05151731 시험 디자인",
        answer,
        (evidence,),
        (),
    )

    assert supplied_url not in sanitized
    assert trace["removed_unbound_source_lines"] == 1


def test_a_binding_does_not_promote_entity_echoed_in_query_payload() -> None:
    evidence = _clinical_set(
        EvidenceRecord(
            evidence_id="ct:NCT05151731",
            source="clinicaltrials",
            result_kind="structured_clinical_record",
            payload={
                **_clinical_record("NCT05151731").payload,
                "query": "DS-7300a",
            },
        )
    )
    answer = (
        "## 미확인 요소\n"
        "- 질의에 포함된 DS-7300a 관련 정보는 확인하지 못했습니다."
    )

    sanitized, trace = sanitize_bound_surface(
        "NCT05151731 시험 디자인",
        answer,
        (evidence,),
        (),
    )

    assert "DS-7300a" not in sanitized
    assert trace["removed_unbound_lines"] == 1


def test_a_binding_removes_table_when_unbound_header_blocks_entire_table() -> None:
    evidence = _clinical_set(_clinical_record("NCT05151731"))
    answer = (
        "## 핵심 답\nNCT05151731의 시험 설계를 확인했습니다.\n\n"
        "## 인접 시험\n"
        "| DS-7300a | 상태 |\n"
        "| --- | --- |\n"
        "| NCT05151731 | COMPLETED |"
    )

    sanitized, trace = sanitize_bound_surface(
        "NCT05151731 시험 디자인",
        answer,
        (evidence,),
        (),
    )

    assert "DS-7300a" not in sanitized
    assert "## 인접 시험" not in sanitized
    assert "| --- | --- |" not in sanitized
    assert trace["removed_unbound_lines"] >= 1


def test_a_binding_removes_unbound_multiword_entity_from_table_header() -> None:
    evidence = _clinical_set(_clinical_record("NCT05151731"))
    answer = (
        "## 인접 시험\n"
        "| Ifinatamab deruxtecan | 상태 |\n"
        "| --- | --- |\n"
        "| NCT05151731 | COMPLETED |"
    )

    sanitized, trace = sanitize_bound_surface(
        "NCT05151731 시험 디자인",
        answer,
        (evidence,),
        (),
    )

    assert "Ifinatamab deruxtecan" not in sanitized
    assert "## 인접 시험" not in sanitized
    assert trace["removed_unbound_lines"] >= 1


def test_a_binding_preserves_pipe_content_inside_fenced_code() -> None:
    evidence = _clinical_set(_clinical_record("NCT05151731"))
    answer = (
        "## 핵심 답\nNCT05151731의 시험 설계를 확인했습니다.\n\n"
        "```text\n"
        "| literal pipe content |\n"
        "```"
    )

    sanitized, trace = sanitize_bound_surface(
        "NCT05151731 시험 디자인",
        answer,
        (evidence,),
        (),
    )

    assert sanitized == answer
    assert trace["answer_mutation"] is False


def test_a_binding_preserves_unbound_text_inside_fenced_code() -> None:
    evidence = _clinical_set(_clinical_record("NCT05151731"))
    answer = (
        "## 핵심 답\nNCT05151731의 시험 설계를 확인했습니다.\n\n"
        "```text\n"
        "DS-7300a is a literal example, not a surfaced claim.\n"
        "```"
    )

    sanitized, trace = sanitize_bound_surface(
        "NCT05151731 시험 디자인",
        answer,
        (evidence,),
        (),
    )

    assert sanitized == answer
    assert trace["answer_mutation"] is False


def test_a_binding_preserves_unbound_text_inside_tilde_fenced_code() -> None:
    evidence = _clinical_set(_clinical_record("NCT05151731"))
    answer = (
        "## 핵심 답\nNCT05151731의 시험 설계를 확인했습니다.\n\n"
        "~~~text\n"
        "DS-7300a is a literal example, not a surfaced claim.\n"
        "~~~"
    )

    sanitized, trace = sanitize_bound_surface(
        "NCT05151731 시험 디자인",
        answer,
        (evidence,),
        (),
    )

    assert sanitized == answer
    assert trace["answer_mutation"] is False


def test_a_binding_requires_a_closing_fence_as_long_as_the_opener() -> None:
    evidence = _clinical_set(_clinical_record("NCT05151731"))
    answer = (
        "## 핵심 답\nNCT05151731의 시험 설계를 확인했습니다.\n\n"
        "~~~~text\n"
        "DS-7300a is a literal example.\n"
        "~~~\n"
        "Ifinatamab deruxtecan is still literal content.\n"
        "~~~~"
    )

    sanitized, trace = sanitize_bound_surface(
        "NCT05151731 시험 디자인",
        answer,
        (evidence,),
        (),
    )

    assert sanitized == answer
    assert trace["answer_mutation"] is False


@pytest.mark.parametrize("fence", ("```", "~~~"))
def test_a_binding_preserves_blockquoted_fenced_literal_content(fence: str) -> None:
    evidence = _clinical_set(_clinical_record("NCT05151731"))
    answer = (
        "## 핵심 답\nNCT05151731의 시험 설계를 확인했습니다.\n\n"
        f"> {fence}text\n"
        "> DS-7300a is a literal example, not a surfaced claim.\n"
        f"> {fence}"
    )

    sanitized, trace = sanitize_bound_surface(
        "NCT05151731 시험 디자인",
        answer,
        (evidence,),
        (),
    )

    assert sanitized == answer
    assert trace["answer_mutation"] is False


def test_a_binding_does_not_promote_nested_request_metadata_url() -> None:
    supplied_url = "https://example.invalid/request-metadata"
    evidence = _clinical_set(
        EvidenceRecord(
            evidence_id="ct:NCT05151731",
            source="clinicaltrials",
            result_kind="structured_clinical_record",
            payload={
                **_clinical_record("NCT05151731").payload,
                "request": {"parameters": {"url": supplied_url}},
            },
        )
    )
    answer = (
        "## 핵심 답\nNCT05151731의 시험 설계를 확인했습니다.\n\n"
        "## 출처\n"
        f"- [요청 메타데이터]({supplied_url})"
    )

    sanitized, trace = sanitize_bound_surface(
        "NCT05151731 시험 디자인",
        answer,
        (evidence,),
        (),
    )

    assert supplied_url not in sanitized
    assert trace["removed_unbound_source_lines"] == 1


def test_a_binding_does_not_invent_core_section_without_binding_deletion() -> None:
    evidence = _clinical_set(_clinical_record("NCT05151731"))
    answer = "## 근거와 맥락\nNCT05151731의 시험 설계를 확인했습니다."

    sanitized, trace = sanitize_bound_surface(
        "NCT05151731 시험 디자인",
        answer,
        (evidence,),
        (),
    )

    assert sanitized == answer
    assert trace["answer_mutation"] is False
    assert trace["core_section_recovered_from"] is None


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


def test_c_claim_ir_keeps_unpunctuated_korean_bullets_separate() -> None:
    evidence = _clinical_set(_clinical_record("NCT05151731"))

    classified = classify_answer_claims(
        "- NCT05151731의 상태는 COMPLETED\n- 해석 근거는 확립되지 않음",
        (evidence,),
    )

    assert [claim.claim_type for claim in classified.claim_ir] == ["T1", "T3"]


def test_c_claim_ir_requires_exact_numeric_support() -> None:
    evidence = _clinical_set(
        EvidenceRecord(
            evidence_id="ct:NCT05151731",
            source="clinicaltrials",
            result_kind="structured_clinical_record",
            payload={"nct_id": "NCT05151731", "response_rate": "120%"},
        )
    )

    classified = classify_answer_claims(
        "NCT05151731의 반응률은 20%입니다.",
        (evidence,),
    )

    assert classified.claim_ir[0].claim_type == "T3"


def test_c_claim_ir_does_not_support_numeric_fragments_or_drop_signs() -> None:
    evidence = _clinical_set(
        EvidenceRecord(
            evidence_id="ct:NCT05151731",
            source="clinicaltrials",
            result_kind="structured_clinical_record",
            payload={
                "nct_id": "NCT05151731",
                "sales_krw": "1,200",
                "delta_krw": "-5",
            },
        )
    )

    fragment = classify_answer_claims(
        "NCT05151731의 매출은 200입니다.",
        (evidence,),
    )
    unsigned = classify_answer_claims(
        "NCT05151731의 변화량은 5입니다.",
        (evidence,),
    )

    assert fragment.claim_ir[0].claim_type == "T3"
    assert unsigned.claim_ir[0].claim_type == "T3"


def test_c_claim_ir_rejects_unbound_company_name() -> None:
    evidence = _clinical_set(_clinical_record("NCT05151731", sponsor="Yuhan"))

    classified = classify_answer_claims(
        "NCT05151731은 Acme Pharmaceuticals가 후원합니다.",
        (evidence,),
    )

    assert classified.claim_ir[0].claim_type == "T3"


def test_c_claim_ir_recomputes_numeric_and_temporal_relations() -> None:
    evidence = _clinical_set(
        EvidenceRecord(
            evidence_id="ct:alpha",
            source="clinicaltrials",
            result_kind="structured_clinical_record",
            payload={"brand": "Alpha", "share": "20%", "start_date": "2023-01-01"},
        ),
        EvidenceRecord(
            evidence_id="ct:beta",
            source="clinicaltrials",
            result_kind="structured_clinical_record",
            payload={"brand": "Beta", "share": "10%", "start_date": "2024-01-01"},
        ),
    )

    classified = classify_answer_claims(
        "Alpha의 20%는 Beta의 10%보다 높습니다. "
        "Alpha의 2023-01-01은 Beta의 2024-01-01보다 먼저입니다.",
        (evidence,),
    )

    assert [claim.claim_type for claim in classified.claim_ir] == ["T2", "T2"]
    assert [claim.operator_id for claim in classified.claim_ir] == [
        "numeric_comparison",
        "temporal_order",
    ]
    assert all(item["result"]["matched"] for item in classified.recomputation_evidence)


def test_c_claim_ir_ignores_request_metadata_values() -> None:
    evidence = _clinical_set(
        EvidenceRecord(
            evidence_id="ct:NCT05151731",
            source="clinicaltrials",
            result_kind="structured_clinical_record",
            payload={
                "nct_id": "NCT05151731",
                "request": {"query": {"entity": "DS-7300a"}},
            },
        )
    )

    classified = classify_answer_claims(
        "DS-7300a는 확인된 시험 물질입니다.",
        (evidence,),
    )

    assert classified.claim_ir[0].claim_type == "T3"


def test_c_claim_ir_rejects_cross_dimension_numeric_relation() -> None:
    evidence = _clinical_set(
        EvidenceRecord(
            evidence_id="ct:alpha",
            source="clinicaltrials",
            result_kind="structured_clinical_record",
            payload={"brand": "Alpha", "sales_krw": "100"},
        ),
        EvidenceRecord(
            evidence_id="ct:beta",
            source="clinicaltrials",
            result_kind="structured_clinical_record",
            payload={"brand": "Beta", "market_share": "10%"},
        ),
    )

    classified = classify_answer_claims(
        "Alpha의 100은 Beta의 10%보다 높습니다.",
        (evidence,),
    )

    assert classified.claim_ir[0].claim_type == "T3"


def test_c_claim_ir_rejects_cross_dimension_temporal_relation() -> None:
    evidence = _clinical_set(
        EvidenceRecord(
            evidence_id="ct:alpha",
            source="clinicaltrials",
            result_kind="structured_clinical_record",
            payload={"brand": "Alpha", "start_date": "2023-01-01"},
        ),
        EvidenceRecord(
            evidence_id="ct:beta",
            source="clinicaltrials",
            result_kind="structured_clinical_record",
            payload={"brand": "Beta", "completion_date": "2024-01-01"},
        ),
    )

    classified = classify_answer_claims(
        "Alpha의 2023-01-01은 Beta의 2024-01-01보다 먼저입니다.",
        (evidence,),
    )

    assert classified.claim_ir[0].claim_type == "T3"


def test_c_claim_ir_rejects_cross_currency_numeric_relation() -> None:
    evidence = _clinical_set(
        EvidenceRecord(
            evidence_id="ct:alpha",
            source="clinicaltrials",
            result_kind="structured_clinical_record",
            payload={"brand": "Alpha", "sales_krw": "100"},
        ),
        EvidenceRecord(
            evidence_id="ct:beta",
            source="clinicaltrials",
            result_kind="structured_clinical_record",
            payload={"brand": "Beta", "sales_usd": "10"},
        ),
    )

    classified = classify_answer_claims(
        "Alpha의 100은 Beta의 10보다 높습니다.",
        (evidence,),
    )

    assert classified.claim_ir[0].claim_type == "T3"


def test_c_claim_ir_rejects_jpy_eur_numeric_relation() -> None:
    evidence = _clinical_set(
        EvidenceRecord(
            evidence_id="ct:alpha",
            source="clinicaltrials",
            result_kind="structured_clinical_record",
            payload={"brand": "Alpha", "sales_jpy": "1200"},
        ),
        EvidenceRecord(
            evidence_id="ct:beta",
            source="clinicaltrials",
            result_kind="structured_clinical_record",
            payload={"brand": "Beta", "sales_eur": "10"},
        ),
    )

    classified = classify_answer_claims(
        "Alpha의 1200은 Beta의 10보다 높습니다.",
        (evidence,),
    )

    assert classified.claim_ir[0].claim_type == "T3"


def test_c_claim_ir_rejects_arbitrary_cross_currency_numeric_relation() -> None:
    evidence = _clinical_set(
        EvidenceRecord(
            evidence_id="ct:alpha",
            source="clinicaltrials",
            result_kind="structured_clinical_record",
            payload={"brand": "Alpha", "sales_cad": "1200"},
        ),
        EvidenceRecord(
            evidence_id="ct:beta",
            source="clinicaltrials",
            result_kind="structured_clinical_record",
            payload={"brand": "Beta", "sales_aud": "10"},
        ),
    )

    classified = classify_answer_claims(
        "Alpha의 1200은 Beta의 10보다 높습니다.",
        (evidence,),
    )

    assert classified.claim_ir[0].claim_type == "T3"


@pytest.mark.parametrize("fence", ("```", "~~~"))
def test_c_claim_ir_excludes_fenced_literal_content(fence: str) -> None:
    evidence = _clinical_set(
        EvidenceRecord(
            evidence_id="ct:alpha",
            source="clinicaltrials",
            result_kind="structured_clinical_record",
            payload={"brand": "Alpha", "status": "COMPLETED"},
        )
    )
    answer = (
        "Alpha의 상태는 COMPLETED입니다.\n\n"
        f"{fence}text\n"
        "DS-7300a causes an unsupported literal example.\n"
        f"{fence}"
    )

    classified = classify_answer_claims(answer, (evidence,))

    assert len(classified.claim_ir) == 1
    assert classified.claim_ir[0].claim_type == "T1"


@pytest.mark.parametrize("fence", ("```", "~~~"))
def test_c_claim_ir_excludes_blockquoted_fenced_literal_content(fence: str) -> None:
    evidence = _clinical_set(
        EvidenceRecord(
            evidence_id="ct:alpha",
            source="clinicaltrials",
            result_kind="structured_clinical_record",
            payload={"brand": "Alpha", "status": "COMPLETED"},
        )
    )
    answer = (
        "Alpha의 상태는 COMPLETED입니다.\n\n"
        f"> {fence}text\n"
        "> DS-7300a causes an unsupported literal example.\n"
        f"> {fence}"
    )

    classified = classify_answer_claims(answer, (evidence,))

    assert len(classified.claim_ir) == 1
    assert classified.claim_ir[0].claim_type == "T1"


def test_g_density_metrics_bind_records_claims_and_populated_fields() -> None:
    evidence = _clinical_set(
        EvidenceRecord(
            evidence_id="ct:NCT05151731",
            source="clinicaltrials",
            result_kind="structured_clinical_record",
            payload={
                "nct_id": "NCT05151731",
                "status": "COMPLETED",
                "sponsor": "원천 미제공",
                "request": {"query": "metadata is not evidence"},
            },
        ),
        EvidenceRecord(
            evidence_id="ct:NCT07470125",
            source="clinicaltrials",
            result_kind="structured_clinical_record",
            payload={
                "nct_id": "NCT07470125",
                "status": "RECRUITING",
                "sponsor": "Yuhan",
            },
        ),
    )

    classified = classify_answer_claims(
        "NCT05151731의 상태는 COMPLETED입니다. 해석 근거는 확립되지 않았습니다.",
        (evidence,),
    )
    metrics = classified.density_metrics

    assert metrics["narrative_record_coverage"] == 0.5
    assert metrics["validated_claims_per_1k_chars"] > 0
    assert metrics["claim_type_counts"] == {"T1": 1, "T2": 0, "T3": 1}
    assert metrics["claim_type_ratio"] == {"T1": 0.5, "T2": 0.0, "T3": 0.5}
    assert metrics["field_coverage_by_source"]["clinicaltrials"] == {
        "records": 2,
        "populated_fields": 5,
        "target_fields": 6,
        "populated_field_rate": 0.833333,
    }


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


@pytest.mark.parametrize("status", ("429", "http_429", "too_many_requests"))
def test_d_http_429_signals_are_classified_as_quota(status: str) -> None:
    assert classify_failure_signals((status,), "") == "quota"


def test_d_external_missing_key_payload_is_not_usable() -> None:
    from jw_chat_agent_poc.service.v4 import adapters as v4_adapters

    missing = SimpleNamespace(status="missing_key")
    live = SimpleNamespace(status="live")
    payload = {"render_data": {"diagnostic": "present"}}

    assert v4_adapters._external_call_is_usable(missing, payload) is False
    assert v4_adapters._external_call_is_usable(live, payload) is True


@pytest.mark.parametrize("nested_status", ("429", "http_429", "too_many_requests"))
def test_d_nested_quota_payload_is_not_usable(nested_status: str) -> None:
    from jw_chat_agent_poc.service.v4 import adapters as v4_adapters

    live = SimpleNamespace(status="live")
    payload = {
        "render_data": {
            "status": nested_status,
            "items": [{"title": "must not count"}],
        }
    }

    assert v4_adapters._external_call_is_usable(live, payload) is False


def test_d_quota_calls_do_not_increase_received_count() -> None:
    result = SourceResult(
        source="web",
        query="synthetic query",
        status="quota",
        payload={
            "calls": [
                {
                    "status": "live",
                    "render_data": {
                        "status": "http_429",
                        "items": [{"title": "must not count"}],
                    },
                }
            ]
        },
        notice="usage limit exceeded",
    )

    event = retrieval_event_from_result(result, entity_id="synthetic")

    assert event.status == "quota"
    assert event.received_count == 0


def test_d_failed_result_top_level_records_do_not_increase_received_count() -> None:
    result = SourceResult(
        source="web",
        query="synthetic query",
        status="quota",
        payload={"items": [{"title": "must not count"}]},
        notice="usage limit exceeded",
    )

    event = retrieval_event_from_result(result, entity_id="synthetic")

    assert event.received_count == 0


def test_d_http_429_notice_is_classified_as_quota() -> None:
    assert classify_failure_signals(("error",), "HTTP 429 Too Many Requests") == "quota"


def test_d_external_status_values_include_nested_provider_status() -> None:
    from jw_chat_agent_poc.service.v4 import adapters as v4_adapters

    call = SimpleNamespace(status="live")
    payload = {"render_data": {"status": "http_429"}}

    assert v4_adapters._external_status_values(call, payload) == (
        "live",
        "http_429",
    )


def test_d_empty_status_with_timeout_notice_is_not_authoritative_absence() -> None:
    result = SourceResult(
        source="web",
        query="synthetic query",
        status="empty",
        notice="HTTPConnectionPool read timed out",
    )

    event = retrieval_event_from_result(result, entity_id="synthetic")
    surface = public_retrieval_notice(event)

    assert event.status == "timeout"
    assert event.reason_code == "timeout"
    assert "0건" not in surface


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

    adapters = {
        source: ok
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


def test_e_tier_zero_finishes_before_auxiliary_adapter_starts() -> None:
    observed: dict[str, float] = {}

    def adapter(source: str, delay: float = 0.0):
        def call(query: str) -> SourceResult:
            observed[f"{source}_start"] = time.monotonic()
            if delay:
                time.sleep(delay)
            observed[f"{source}_end"] = time.monotonic()
            return SourceResult(source=source, query=query, status="ok")

        return call

    adapters = {
        source: adapter(source, 0.05 if source == "mart" else 0.0)
        for source in SOURCE_NAMES
    }
    plan = _plan("리바로 매출", answer_sources=("mart",))

    ParallelSourceExecutor(adapters=adapters).execute_with_trace(
        plan,
        session_id="r12-2-tier-order",
        source_filter=("mart", "web"),
    )

    assert observed["web_start"] >= observed["mart_end"]


def test_e_running_tier_zero_timeout_does_not_release_auxiliary_gate() -> None:
    observed: dict[str, float] = {}

    def adapter(source: str):
        def call(query: str) -> SourceResult:
            observed[f"{source}_start"] = time.monotonic()
            if source == "mart":
                time.sleep(0.15)
            observed[f"{source}_end"] = time.monotonic()
            return SourceResult(source=source, query=query, status="ok")

        return call

    adapters = {source: adapter(source) for source in SOURCE_NAMES}
    plan = _plan("리바로 매출", answer_sources=("mart",))

    ParallelSourceExecutor(
        adapters=adapters,
        per_tool_timeout_s=0.05,
        total_timeout_s=0.35,
    ).execute_with_trace(
        plan,
        session_id="r12-2-running-tier-zero-timeout",
        source_filter=("mart", "web"),
    )

    assert observed["web_start"] >= observed["mart_end"]


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


def test_f_entity_completion_depends_on_tier_zero_not_auxiliary_success() -> None:
    plan = _plan(
        "리바로젯 매출 현황",
        answer_sources=("mart",),
        entities=("리바로젯",),
    )

    failed_primary = entity_completion_rows(
        plan,
        (
            SourceResult(source="mart", query="리바로젯 매출", status="timeout"),
            SourceResult(source="web", query="리바로젯 뉴스", status="ok"),
        ),
    )
    successful_primary = entity_completion_rows(
        plan,
        (
            SourceResult(source="mart", query="리바로젯 매출", status="ok"),
            SourceResult(source="web", query="리바로젯 뉴스", status="timeout"),
        ),
    )

    assert failed_primary.rows[0]["status"] == "FAILED"
    assert successful_primary.rows[0]["status"] == "COMPLETE"


def test_f_entity_completion_ignores_request_query_echo() -> None:
    plan = _plan(
        "Alpha, Beta 매출 현황",
        answer_sources=("mart",),
        entities=("Alpha", "Beta"),
    )
    result = SourceResult(
        source="mart",
        query="Alpha Beta sales",
        status="ok",
        payload={
            "rows": [{"brand": "Alpha", "sales_krw": "100"}],
            "request": {"query": "Alpha Beta"},
        },
    )

    coverage = entity_completion_rows(plan, (result,))

    assert [row["status"] for row in coverage.rows] == ["COMPLETE", "FAILED"]


def test_f_entity_completion_does_not_fallback_to_query_when_payload_has_other_entity() -> None:
    plan = _plan(
        "Alpha 매출 현황",
        answer_sources=("mart",),
        entities=("Alpha",),
    )
    result = SourceResult(
        source="mart",
        query="Alpha sales",
        status="ok",
        payload={"rows": [{"brand": "Gamma", "sales_krw": "100"}]},
    )

    coverage = entity_completion_rows(plan, (result,))

    assert coverage.rows[0]["status"] == "FAILED"


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


def test_h_resolver_first_preserves_transport_safe_planner_filters() -> None:
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


def test_h_multi_entity_preparation_keeps_one_request_per_entity() -> None:
    planner = ClinicalTrialConcept(
        countries=("Korea",),
        source_queries=("planner query",),
    )
    pairs = (
        (
            "리바로젯",
            SimpleNamespace(
                canonical_brand="리바로젯",
                molecule_en=("pitavastatin", "ezetimibe"),
            ),
        ),
        (
            "리피토",
            SimpleNamespace(
                canonical_brand="리피토",
                molecule_en=("atorvastatin",),
            ),
        ),
        (
            "리바로",
            SimpleNamespace(
                canonical_brand="리바로",
                molecule_en=("pitavastatin",),
            ),
        ),
    )

    prepared = prepare_resolved_clinical_requests(
        pairs,
        (planner,),
        scope_query="리바로젯, 리피토, 리바로 매출 현황",
    )

    assert [query for query, _concept in prepared] == ["리바로젯", "리피토", "리바로"]
    parameters = [compile_clinical_query(concept).parameters for _query, concept in prepared]
    assert len(parameters) == 3
    assert all(not any("가" <= char <= "힣" for char in str(item)) for item in parameters)


def test_h_multi_entity_preparation_preserves_entities_with_same_parameters() -> None:
    pairs = (
        (
            "리바로",
            SimpleNamespace(
                canonical_brand="리바로",
                molecule_en=("pitavastatin",),
            ),
        ),
        (
            "피타로우",
            SimpleNamespace(
                canonical_brand="피타로우",
                molecule_en=("pitavastatin",),
            ),
        ),
    )

    prepared = prepare_resolved_clinical_requests(
        pairs,
        (),
        scope_query="리바로, 피타로우 임상현황",
    )

    assert [query for query, _concept in prepared] == ["리바로", "피타로우"]
    assert len(prepared) == 2


def test_h_preparation_derives_explicit_scope_without_planner_variance() -> None:
    pairs = (
        (
            "리바로젯",
            SimpleNamespace(
                canonical_brand="리바로젯",
                molecule_en=("pitavastatin", "ezetimibe"),
            ),
        ),
    )
    first_planner = ClinicalTrialConcept(
        countries=("Japan",),
        statuses=("COMPLETED",),
        source_queries=("Livarozet generic",),
    )
    second_planner = ClinicalTrialConcept(
        countries=("Korea",),
        statuses=("RECRUITING",),
        source_queries=("Pitavastatin Ezetimibe",),
    )
    question = "국내 모집 중 리바로젯 임상현황"

    first = prepare_resolved_clinical_requests(
        pairs,
        (first_planner,),
        scope_query=question,
    )
    second = prepare_resolved_clinical_requests(
        pairs,
        (second_planner,),
        scope_query=question,
    )

    first_parameters = [compile_clinical_query(item).parameters for _query, item in first]
    second_parameters = [compile_clinical_query(item).parameters for _query, item in second]
    assert first_parameters == second_parameters
    assert first_parameters[0]["query.locn"] == "Korea"
    assert first_parameters[0]["filter.overallStatus"] == "RECRUITING"


def test_h_active_session_scope_overrides_planner_completed_status() -> None:
    pairs = (
        (
            "리바로젯",
            SimpleNamespace(
                canonical_brand="리바로젯",
                molecule_en=("pitavastatin", "ezetimibe"),
            ),
        ),
    )
    planner = ClinicalTrialConcept(
        statuses=("COMPLETED",),
        source_queries=("LivaloZet completed",),
    )

    prepared = prepare_resolved_clinical_requests(
        pairs,
        (planner,),
        scope_query="국내 진행 중 리바로젯 임상현황",
    )
    parameters = [compile_clinical_query(item).parameters for _query, item in prepared]

    assert parameters[0]["query.locn"] == "Korea"
    assert parameters[0]["filter.overallStatus"] == "RECRUITING"


def test_h_question_grounded_planner_concept_is_supplement_only() -> None:
    pairs = (
        (
            "LivaloZet",
            SimpleNamespace(
                canonical_brand="LivaloZet",
                molecule_en=("pitavastatin", "ezetimibe"),
            ),
        ),
    )
    planner = ClinicalTrialConcept(
        ingredients=("hyperlipidemia",),
        search_area="condition",
        source_queries=("hyperlipidemia",),
    )

    prepared = prepare_resolved_clinical_requests(
        pairs,
        (planner,),
        scope_query="LivaloZet hyperlipidemia clinical trials",
    )

    parameters = [compile_clinical_query(item).parameters for _query, item in prepared]
    assert parameters[0]["query.intr"] == "pitavastatin OR ezetimibe"
    assert parameters[1]["query.cond"] == "hyperlipidemia"


def test_h_planner_supplement_inherits_explicit_scope() -> None:
    pairs = (
        (
            "LivaloZet",
            SimpleNamespace(
                canonical_brand="LivaloZet",
                molecule_en=("pitavastatin", "ezetimibe"),
            ),
        ),
    )
    planner = ClinicalTrialConcept(
        ingredients=("hyperlipidemia",),
        search_area="condition",
        countries=("Japan",),
        statuses=("COMPLETED",),
        source_queries=("hyperlipidemia",),
    )

    prepared = prepare_resolved_clinical_requests(
        pairs,
        (planner,),
        scope_query="국내 모집 중 LivaloZet hyperlipidemia clinical trials",
    )

    parameters = [compile_clinical_query(item).parameters for _query, item in prepared]
    assert len(parameters) == 2
    assert all(item["query.locn"] == "Korea" for item in parameters)
    assert all(item["filter.overallStatus"] == "RECRUITING" for item in parameters)


def test_h_generic_source_query_does_not_ground_fabricated_supplement() -> None:
    pairs = (
        (
            "LivaloZet",
            SimpleNamespace(
                canonical_brand="LivaloZet",
                molecule_en=("pitavastatin", "ezetimibe"),
            ),
        ),
    )
    fabricated = ClinicalTrialConcept(
        ingredients=("Madeupdrug",),
        source_queries=("clinical trials",),
    )

    prepared = prepare_resolved_clinical_requests(
        pairs,
        (fabricated,),
        scope_query="LivaloZet clinical trials",
    )

    assert len(prepared) == 1
    assert "Madeupdrug" not in str(compile_clinical_query(prepared[0][1]).parameters)


def test_h_partly_grounded_planner_supplement_is_rejected_as_a_whole() -> None:
    pairs = (
        (
            "LivaloZet",
            SimpleNamespace(
                canonical_brand="LivaloZet",
                molecule_en=("pitavastatin", "ezetimibe"),
            ),
        ),
    )
    mixed = ClinicalTrialConcept(
        ingredients=("hyperlipidemia", "Madeupdrug"),
        search_area="condition",
        source_queries=("hyperlipidemia Madeupdrug",),
    )

    prepared = prepare_resolved_clinical_requests(
        pairs,
        (mixed,),
        scope_query="LivaloZet hyperlipidemia clinical trials",
    )

    assert len(prepared) == 1
    assert "Madeupdrug" not in str(prepared)


def test_h_planner_supplement_requires_token_grounding_and_explicit_filters() -> None:
    pairs = (
        (
            "LivaloZet",
            SimpleNamespace(
                canonical_brand="LivaloZet",
                molecule_en=("pitavastatin", "ezetimibe"),
            ),
        ),
    )
    partial = ClinicalTrialConcept(
        brands=("Liva",),
        countries=("Japan",),
        statuses=("COMPLETED",),
        source_queries=("Liva",),
    )

    prepared = prepare_resolved_clinical_requests(
        pairs,
        (partial,),
        scope_query="LivaloZet clinical trials",
    )
    parameters = [compile_clinical_query(item).parameters for _query, item in prepared]

    assert len(parameters) == 1
    assert all("query.locn" not in item for item in parameters)
    assert all("filter.overallStatus" not in item for item in parameters)


def test_h_question_explicit_non_korean_scope_is_preserved() -> None:
    pairs = (
        (
            "LivaloZet",
            SimpleNamespace(
                canonical_brand="LivaloZet",
                molecule_en=("pitavastatin", "ezetimibe"),
            ),
        ),
    )
    planner = ClinicalTrialConcept(
        countries=("Japan",),
        statuses=("COMPLETED",),
        source_queries=("LivaloZet",),
    )

    prepared = prepare_resolved_clinical_requests(
        pairs,
        (planner,),
        scope_query="Japan completed LivaloZet clinical trials",
    )
    parameters = [compile_clinical_query(item).parameters for _query, item in prepared]

    assert parameters[0]["query.locn"] == "Japan"
    assert parameters[0]["filter.overallStatus"] == "COMPLETED"


def test_h_question_explicit_japanese_scope_is_preserved() -> None:
    pairs = (
        (
            "LivaloZet",
            SimpleNamespace(
                canonical_brand="LivaloZet",
                molecule_en=("pitavastatin", "ezetimibe"),
            ),
        ),
    )
    planner = ClinicalTrialConcept(
        countries=("Japan",),
        statuses=("COMPLETED",),
        source_queries=("LivaloZet",),
    )

    prepared = prepare_resolved_clinical_requests(
        pairs,
        (planner,),
        scope_query="일본 완료 LivaloZet 임상시험",
    )
    parameters = [compile_clinical_query(item).parameters for _query, item in prepared]

    assert parameters[0]["query.locn"] == "Japan"
    assert parameters[0]["filter.overallStatus"] == "COMPLETED"


def test_h_live_adapter_executes_prepared_planner_supplement_without_reresolving(
    monkeypatch,
) -> None:
    from jw_chat_agent_poc.agent_loop import factory
    from jw_chat_agent_poc.service import general_view_routing
    from jw_chat_agent_poc.service.v4 import adapters as v4_adapters
    from jw_chat_agent_poc.tools.external.client import ExternalCall

    captured: list[ClinicalTrialConcept] = []

    class Resolver:
        def resolve(self, _query, *, allow_default):
            assert allow_default is False
            return SimpleNamespace(
                canonical_brand="LivaloZet",
                molecule_en=("pitavastatin", "ezetimibe"),
                market_ids=(),
            )

    class External:
        timeout_s = 12

    def clinical_call(
        _query: str,
        concept: ClinicalTrialConcept,
        *,
        timeout_s: float,
    ) -> ExternalCall:
        assert timeout_s == 12
        captured.append(concept)
        return ExternalCall(
            tool="clinicaltrials_v2_lossless_search",
            source="clinicaltrials_api_v2",
            status="no_data",
            summary_text="no records",
            render_data={"payload": {"studies": []}},
            safe_url="https://clinicaltrials.gov/api/v2/studies",
        )

    monkeypatch.setattr(
        factory,
        "build_chat_agent_dependencies",
        lambda **_kwargs: SimpleNamespace(
            external=External(),
            resolver=Resolver(),
            query_layer=None,
        ),
    )
    monkeypatch.setattr(
        general_view_routing.GeneralViewService,
        "from_env",
        lambda _resolver: SimpleNamespace(),
    )
    monkeypatch.setattr(v4_adapters, "_clinical_lossless_external_call", clinical_call)
    supplement = ClinicalTrialConcept(
        ingredients=("hyperlipidemia",),
        search_area="condition",
        source_queries=("hyperlipidemia",),
    )

    v4_adapters.build_source_adapters()["clinicaltrials"](
        "LivaloZet hyperlipidemia clinical trials",
        concept=supplement,
    )

    assert captured == [supplement]


def test_h_live_adapter_preparer_resolves_each_entity_once(monkeypatch) -> None:
    from jw_chat_agent_poc.agent_loop import factory
    from jw_chat_agent_poc.service import general_view_routing
    from jw_chat_agent_poc.service.v4 import adapters as v4_adapters

    resolutions = {
        "리바로젯": SimpleNamespace(
            canonical_brand="리바로젯",
            molecule_en=("pitavastatin", "ezetimibe"),
        ),
        "리피토": SimpleNamespace(
            canonical_brand="리피토",
            molecule_en=("atorvastatin",),
        ),
        "리바로": SimpleNamespace(
            canonical_brand="리바로",
            molecule_en=("pitavastatin",),
        ),
    }

    class Resolver:
        def resolve(self, query, *, allow_default):
            assert allow_default is False
            if query not in resolutions:
                raise LookupError(query)
            return resolutions[query]

    monkeypatch.setattr(
        factory,
        "build_chat_agent_dependencies",
        lambda **_kwargs: SimpleNamespace(
            external=SimpleNamespace(timeout_s=12),
            resolver=Resolver(),
            query_layer=None,
        ),
    )
    monkeypatch.setattr(
        general_view_routing.GeneralViewService,
        "from_env",
        lambda _resolver: SimpleNamespace(),
    )
    adapter = v4_adapters.build_source_adapters()["clinicaltrials"]
    prepare = getattr(adapter, "prepare_requests")

    prepared = prepare(
        "리바로젯, 리피토, 리바로 매출 현황",
        (ClinicalTrialConcept(source_queries=("planner",)),),
    )

    assert [query for query, _concept in prepared] == ["리바로젯", "리피토", "리바로"]
    assert len(prepared) == 3


def test_h_live_adapter_preserves_aliases_with_identical_clinical_parameters(
    monkeypatch,
) -> None:
    from jw_chat_agent_poc.agent_loop import factory
    from jw_chat_agent_poc.service import general_view_routing
    from jw_chat_agent_poc.service.v4 import adapters as v4_adapters

    resolutions = {
        entity: SimpleNamespace(
            canonical_brand="pitavastatin-brand",
            molecule_en=("pitavastatin",),
        )
        for entity in ("리바로", "피타로우")
    }

    class Resolver:
        def resolve(self, query, *, allow_default):
            assert allow_default is False
            if query not in resolutions:
                raise LookupError(query)
            return resolutions[query]

    monkeypatch.setattr(
        factory,
        "build_chat_agent_dependencies",
        lambda **_kwargs: SimpleNamespace(
            external=SimpleNamespace(timeout_s=12),
            resolver=Resolver(),
            query_layer=None,
        ),
    )
    monkeypatch.setattr(
        general_view_routing.GeneralViewService,
        "from_env",
        lambda _resolver: SimpleNamespace(),
    )
    adapter = v4_adapters.build_source_adapters()["clinicaltrials"]

    prepared = adapter.prepare_requests(
        "리바로, 피타로우 임상현황",
        (),
    )

    assert [query for query, _concept in prepared] == ["리바로", "피타로우"]


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


def test_h_planner_variance_is_normalized_only_for_execution() -> None:
    question = "리바로젯 제네릭 임상현황"
    base = _plan(question)
    first_plan = base.model_copy(
        update={
            "tool_queries": base.tool_queries.model_copy(
                update={"clinicaltrials": ("Pitavastatin Ezetimibe",)}
            ),
            "clinical_query_specs": (
                ClinicalTrialConcept(
                    ingredients=("Pitavastatin", "Ezetimibe"),
                    source_queries=("Pitavastatin Ezetimibe",),
                ),
            ),
        }
    )
    second_plan = base.model_copy(
        update={
            "tool_queries": base.tool_queries.model_copy(
                update={"clinicaltrials": ("Livarozet generic",)}
            ),
            "clinical_query_specs": (
                ClinicalTrialConcept(
                    brands=("Livarozet",),
                    source_queries=("Livarozet generic",),
                ),
            ),
        }
    )

    first = _attach_lossless_contracts(question, first_plan)
    second = _attach_lossless_contracts(question, second_plan)

    assert first.tool_queries.clinicaltrials == ("Pitavastatin Ezetimibe",)
    assert second.tool_queries.clinicaltrials == ("Livarozet generic",)

    canonical = ClinicalTrialConcept(
        ingredients=("ezetimibe", "pitavastatin"),
        source_queries=(question,),
    )

    def clinical_adapter(query: str, *, concept=None) -> SourceResult:
        return SourceResult(source="clinicaltrials", query=query, status="empty")

    def prepare_requests(anchor: str, _concepts) -> tuple[tuple[str, ClinicalTrialConcept], ...]:
        return ((anchor, canonical),)

    setattr(clinical_adapter, "prepare_requests", prepare_requests)
    adapters = {
        source: (
            clinical_adapter
            if source == "clinicaltrials"
            else lambda query, source=source: SourceResult(
                source=source,
                query=query,
                status="empty",
            )
        )
        for source in SOURCE_NAMES
    }
    executor = ParallelSourceExecutor(adapters=adapters)

    first_execution = executor.prepare_plan(first, clinical_query_anchor=question)
    second_execution = executor.prepare_plan(second, clinical_query_anchor=question)

    assert first_execution.tool_queries.clinicaltrials == (question,)
    assert second_execution.tool_queries.clinicaltrials == (question,)
    assert first_execution.clinical_query_specs == (canonical,)
    assert second_execution.clinical_query_specs == (canonical,)


def test_h_execution_normalization_preserves_first_and_second_hop_provenance() -> None:
    raw_question = "리바로젯 제네릭 임상현황"
    resolved_question = "국내 모집 중 리바로젯 제네릭 임상현황"
    first_plan = _plan(resolved_question).model_copy(
        update={
            "needs_second_hop": True,
            "tool_queries": _plan("리바로젯 제네릭 임상현황").tool_queries.model_copy(
                update={"clinicaltrials": ("planner first query",)}
            ),
        }
    )
    linked_plan = _plan("리바로젯 관련 임상").model_copy(
        update={
            "tool_queries": _plan("리바로젯 관련 임상").tool_queries.model_copy(
                update={"clinicaltrials": ("planner linked query",)}
            ),
        }
    )

    class Planner:
        def plan_with_trace(self, _question, _turns, *, budget_s):
            return SimpleNamespace(
                plan=first_plan,
                trace={"elapsed_ms": 1.0, "usage": {}},
            )

        def link(self, *_args, **_kwargs):
            return linked_plan

    class Executor:
        def __init__(self) -> None:
            self.executed_queries: list[tuple[str, ...]] = []

        def prepare_plan(self, plan, *, clinical_query_anchor):
            normalized = f"normalized:{clinical_query_anchor}"
            return plan.model_copy(
                update={
                    "tool_queries": plan.tool_queries.model_copy(
                        update={"clinicaltrials": (normalized,)}
                    )
                }
            )

        def execute_with_trace(self, plan, **_kwargs):
            self.executed_queries.append(plan.tool_queries.clinicaltrials)
            return SimpleNamespace(
                results=(
                    SourceResult(
                        source="clinicaltrials",
                        query=plan.tool_queries.clinicaltrials[0],
                        status="empty",
                    ),
                ),
                trace={"elapsed_ms": 1.0, "tools": []},
            )

    class Synthesizer:
        def __init__(self) -> None:
            self.plan = None

        def synthesize_with_trace(
            self,
            plan,
            _results,
            _turns,
            *,
            budget_s,
            deterministic_facts,
        ):
            self.plan = plan
            return SynthesisOutcome(
                text="## 핵심 답\n조회 결과가 없습니다.",
                trace={"elapsed_ms": 1.0, "usage": {}},
            )

    executor = Executor()
    synthesizer = Synthesizer()
    answer = V4Runtime(
        planner=Planner(),
        executor=executor,
        synthesizer=synthesizer,
    ).answer(
        raw_question,
        conversation_id="r12-2-normalized-hops",
        turns=(),
    )

    assert executor.executed_queries == [
        ("normalized:리바로젯 제네릭 임상현황",),
        ("normalized:리바로젯 관련 임상",),
    ]
    assert synthesizer.plan.tool_queries.clinicaltrials == ("planner first query",)
    assert answer.trace["planner"]["tool_queries"]["clinicaltrials"] == [
        "planner first query"
    ]
    assert answer.trace["second_hop"]["tool_queries"]["clinicaltrials"] == [
        "planner linked query"
    ]
    assert answer.trace["clinical_query_normalization"]["execution_queries"] == [
        "normalized:리바로젯 제네릭 임상현황"
    ]
    assert answer.trace["linked_clinical_query_normalization"][
        "execution_queries"
    ] == ["normalized:리바로젯 관련 임상"]


def test_h_clinical_anchor_uses_only_user_and_inherited_session_scope() -> None:
    raw_question = "리바로젯 제네릭 임상현황"
    followup = "그 결과 임상현황 알려줘"
    state = SessionState(
        primary_entity="리바로젯",
        record_type="clinical_trial",
        status_filter=("active",),
        country_filter=("KR",),
    )

    assert (
        v4_runtime._deterministic_clinical_query_anchor(raw_question, None)
        == raw_question
    )
    assert v4_runtime._deterministic_clinical_query_anchor(followup, state) == (
        "그 결과 임상현황 알려줘 리바로젯 진행 중 국내"
    )


def test_h_unresolved_korean_query_fails_typed_instead_of_silent_empty() -> None:
    planner = ClinicalTrialConcept(
        brands=("알수없는브랜드",),
        source_queries=("알수없는브랜드 임상",),
    )

    decision = resolver_first_clinical_concepts("알수없는브랜드 임상", None, planner)

    assert decision.concepts == ()
    assert decision.blocked_reason == "unresolved_korean_clinical_query"


def test_h_japanese_country_and_completion_scope_are_preserved() -> None:
    resolution = SimpleNamespace(
        molecule_en=("pitavastatin", "ezetimibe"),
        canonical_brand="LivaloZet",
    )

    prepared = prepare_resolved_clinical_requests(
        (("LivaloZet", resolution),),
        (),
        scope_query="日本 完了 LivaloZet 臨床試験",
    )

    assert len(prepared) == 1
    assert prepared[0][1].countries == ("Japan",)
    assert prepared[0][1].statuses == ("COMPLETED",)


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


@pytest.mark.parametrize(
    "question",
    (
        "효능과 안전성 알려줘",
        "리바로 효능과 안전성 현황",
    ),
)
def test_h_entity_candidates_do_not_split_attribute_conjunctions(question: str) -> None:
    assert query_entity_candidates(question) == ()


@pytest.mark.parametrize(
    "question",
    (
        "한국과 일본 임상 현황",
        "완료와 모집 중 임상시험",
    ),
)
def test_h_scope_and_status_conjunctions_are_not_entities(question: str) -> None:
    assert query_entity_candidates(question) == ()
    assert not ({"한국", "일본", "완료", "모집 중"} & set(_requested_answer_shape(question).entities))


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


def test_f_tier_zero_fanout_preserves_multiple_query_intents() -> None:
    plan = _plan(
        "리바로젯, 리피토, 리바로 매출과 점유율",
        answer_sources=("mart",),
        entities=("리바로젯", "리피토", "리바로"),
    ).model_copy(
        update={
            "tool_queries": _plan("placeholder").tool_queries.model_copy(
                update={"mart": ("시장 매출 현황", "시장 점유율")}
            )
        }
    )

    expanded = fan_out_tier_zero_queries(plan)

    assert expanded.tool_queries.mart == (
        "리바로젯 시장 매출 현황",
        "리피토 시장 매출 현황",
        "리바로 시장 매출 현황",
        "리바로젯 시장 점유율",
        "리피토 시장 점유율",
        "리바로 시장 점유율",
    )
    assert fan_out_tier_zero_queries(expanded) == expanded


def test_f_tier_zero_fanout_completes_entity_intent_cross_product() -> None:
    plan = _plan(
        "A, B 매출과 점유율",
        answer_sources=("mart",),
        entities=("A", "B"),
    ).model_copy(
        update={
            "tool_queries": _plan("placeholder").tool_queries.model_copy(
                update={"mart": ("A 매출 현황", "B 점유율")}
            )
        }
    )

    expanded = fan_out_tier_zero_queries(plan)

    assert expanded.tool_queries.mart == (
        "A 매출 현황",
        "B 매출 현황",
        "A 점유율",
        "B 점유율",
    )
    assert fan_out_tier_zero_queries(expanded) == expanded


def test_f_tier_zero_fanout_drops_korean_conjunction_particle() -> None:
    plan = _plan(
        "리바로젯과 리피토 매출 현황",
        answer_sources=("mart",),
        entities=("리바로젯", "리피토"),
    )

    expanded = fan_out_tier_zero_queries(plan)

    assert expanded.tool_queries.mart == (
        "리바로젯 매출 현황",
        "리피토 매출 현황",
    )


def test_f_linked_execution_plan_applies_tier_zero_fanout() -> None:
    plan = _plan(
        "리바로젯, 리피토 매출 현황",
        answer_sources=("mart",),
        entities=("리바로젯", "리피토"),
    ).model_copy(
        update={
            "tool_queries": _plan("placeholder").tool_queries.model_copy(
                update={"mart": ("시장 매출 현황",)}
            )
        }
    )

    prepared, _trace = v4_runtime._execution_plan(
        SimpleNamespace(),
        plan,
        clinical_query_anchor=plan.resolved_question,
    )

    assert prepared.tool_queries.mart == (
        "리바로젯 시장 매출 현황",
        "리피토 시장 매출 현황",
    )


def test_h_unresolved_korean_preparation_clears_planner_concept() -> None:
    planner_concept = ClinicalTrialConcept(
        brands=("알수없는브랜드",),
        source_queries=("알수없는브랜드 임상",),
    )
    plan = _plan("알수없는브랜드 임상").model_copy(
        update={"clinical_query_specs": (planner_concept,)}
    )

    def clinical_adapter(query: str, *, concept=None) -> SourceResult:
        return SourceResult(source="clinicaltrials", query=query, status="upstream")

    setattr(clinical_adapter, "prepare_requests", lambda _anchor, _concepts: ())
    adapters = {
        source: (
            clinical_adapter
            if source == "clinicaltrials"
            else lambda query, source=source: SourceResult(
                source=source,
                query=query,
                status="empty",
            )
        )
        for source in SOURCE_NAMES
    }
    executor = ParallelSourceExecutor(adapters=adapters)

    prepared = executor.prepare_plan(
        plan,
        clinical_query_anchor=plan.resolved_question,
    )

    assert prepared.clinical_query_specs == ()


def test_h_clinical_scope_suffix_is_canonical_and_deterministic() -> None:
    assert (
        clinical_query_policy.clinical_scope_suffix("국내 모집 중 리바로젯 임상현황")
        == "Korea RECRUITING"
    )


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
            return SimpleNamespace(
                results=(clinical, timeout),
                trace={"elapsed_ms": 1.0, "tools": []},
            )

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
    assert (
        enabled.trace["claim_ir_shadow"]["input_answer_sha256"]
        == enabled.trace["claim_ir_shadow"]["output_answer_sha256"]
    )
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
