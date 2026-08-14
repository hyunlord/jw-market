from __future__ import annotations

from datetime import date
import json
from types import SimpleNamespace

import pytest

from jw_chat_agent_poc.service.v4.lossless_contracts import (
    DeterministicRender,
    CoverageLedger,
    EvidenceRecord,
    EvidenceSet,
    RenderNode,
)
from jw_chat_agent_poc.service.v4.lossless_spine import compose_lossless_answer
from jw_chat_agent_poc.service.v4.contracts import PlannerOutput, SourceResult, ToolQueries
from jw_chat_agent_poc.service.v4.evidence_sets import build_evidence_sets
from jw_chat_agent_poc.service.v4.inspection import build_inspection_detail
from jw_chat_agent_poc.service.v4.narrative_realization import (
    build_narrative_realization,
    measure_final_narrative_surface,
)
from jw_chat_agent_poc.service.v4.render_clinical import render_clinical
from jw_chat_agent_poc.service.v4.synthesizer import (
    _SYNTHESIS_SYSTEM_PROMPT,
    _synthesis_messages,
)
from jw_chat_agent_poc.service.v4 import adapters as v4_adapters
from jw_chat_agent_poc.service.web_relevance import filter_web_results
from jw_chat_agent_poc.tools.external.client import ExternalCall


def _evidence(source: str, *records: EvidenceRecord) -> EvidenceSet:
    return EvidenceSet(
        source=source,
        retrieved_at="2026-08-14T00:00:00Z",
        coverage=CoverageLedger(
            records_received=len(records),
            records_unique=len(records),
            records_relevant=len(records),
        ),
        records=records,
    )


def _plan(question: str = "q") -> PlannerOutput:
    return PlannerOutput(
        resolved_question=question,
        expanded_intents=(question,),
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
    )


@pytest.mark.parametrize(
    ("status", "summary"),
    (
        ("no_data", "조회 결과 없음"),
        ("error", "HTTP 503 upstream failure"),
        ("timeout", "read timed out"),
        ("quota", "usage limit exceeded"),
        ("parse_error", "response schema parse failure"),
    ),
)
def test_b_failed_generic_call_is_failure_only_not_evidence_record(
    status: str,
    summary: str,
) -> None:
    result = SourceResult(
        source="openfda",
        query="리바로젯",
        status="ok",
        payload={
            "calls": [
                {
                    "tool": "openfda_search",
                    "status": status,
                    "summary_text": summary,
                    "render_data": {
                        "status": status,
                        "message": summary,
                    },
                }
            ]
        },
    )

    (evidence_set,) = build_evidence_sets(
        _plan("리바로젯"),
        (result,),
        observed_on=date(2026, 8, 14),
    )

    assert evidence_set.records == ()
    assert len(evidence_set.item_failures) == 1
    assert evidence_set.item_failures[0]["status"] == status


def test_a_narrative_uses_no_source_axis_heading_or_internal_record_index() -> None:
    records = tuple(
        EvidenceRecord(
            evidence_id=f"hira:{index}",
            source="hira",
            result_kind="policy",
            payload={
                "status": "no_data",
                "phase": "UNKNOWN",
                "sponsor": "JW",
            },
        )
        for index in range(1, 3)
    )

    realized = build_narrative_realization(
        (_evidence("hira", *records),),
        tuple(record.evidence_id for record in records),
    )
    surface = "\n".join(node.text for node in realized.nodes)

    assert "## 건강보험심사평가원 요약" not in surface
    assert "확인 레코드" not in surface


def test_a_record_sentence_keeps_sparse_records_and_localizes_enums() -> None:
    sparse = EvidenceRecord(
        evidence_id="ct:sparse",
        source="clinicaltrials",
        result_kind="clinical",
        payload={"nct_id": "NCT00000001", "overall_status": "RECRUITING"},
    )
    complete = EvidenceRecord(
        evidence_id="ct:complete",
        source="clinicaltrials",
        result_kind="clinical",
        payload={
            "nct_id": "NCT00000002",
            "overall_status": "RECRUITING",
            "phase": "PHASE2",
            "sponsor": "JW중외제약",
        },
    )

    realized = build_narrative_realization(
        (_evidence("clinicaltrials", sparse, complete),),
        (sparse.evidence_id, complete.evidence_id),
    )
    t1 = tuple(item for item in realized.claims if item.claim.claim_type == "T1")

    assert len(t1) == 2
    assert "NCT00000001" in t1[0].text
    assert "모집 중" in t1[0].text
    assert "NCT00000002" in t1[1].text
    assert "모집 중" in t1[1].text
    assert "RECRUITING" not in t1[1].text
    assert "PHASE2" not in t1[1].text


def test_a_web_record_uses_exact_inline_citation() -> None:
    record = EvidenceRecord(
        evidence_id="web:1",
        source="web",
        result_kind="web",
        payload={
            "title": "리바로젯 제네릭 경쟁 확대",
            "publisher": "데일리팜",
            "published_at": "2026-08-13",
            "summary": "국내 제네릭 도전 현황을 정리했습니다.",
        },
    )

    realized = build_narrative_realization(
        (_evidence("web", record),),
        (record.evidence_id,),
    )
    surface = "\n".join(node.text for node in realized.nodes)

    assert "[출처: 데일리팜 · 2026-08-13 · 「리바로젯 제네릭 경쟁 확대」]" in surface
    assert "[출처: 웹 뉴스]" not in surface


def test_a_synthesis_contract_does_not_require_fixed_four_sections() -> None:
    assert "반드시 `## 핵심 답`" not in _SYNTHESIS_SYSTEM_PROMPT
    assert "질문에 대한 답을 첫 문장" in _SYNTHESIS_SYSTEM_PROMPT
    assert "출처별 소제목" in _SYNTHESIS_SYSTEM_PROMPT
    assert "명시적인 확인 한계" in _SYNTHESIS_SYSTEM_PROMPT


def test_a_composition_preserves_question_driven_sections_and_deduplicates() -> None:
    rendered = DeterministicRender(
        profile="clinical_portfolio",
        nodes=(
            RenderNode(
                block_id="narrative:field-restatement",
                record_ids=("ct:1",),
                text="NCT00000001은 모집 중입니다. [출처: ClinicalTrials.gov]",
            ),
        ),
    )
    commentary = (
        "리바로젯 관련 임상시험이 확인됐습니다.\n\n"
        "## 경쟁 구도\n첫 번째 관찰입니다.\n\n"
        "## FDA 요약\nFDA 근거도 확인했습니다.\n\n"
        "## 경쟁 구도\n두 번째 관찰입니다."
    )

    composed = compose_lossless_answer(
        rendered,
        commentary,
        synthesis_trace={},
        mode="inject",
    )

    assert composed.text.startswith("NCT00000001은 모집 중입니다.")
    assert "## 핵심 답" not in composed.text
    assert "## 근거와 맥락" not in composed.text
    assert "## FDA 요약" not in composed.text
    assert "FDA 근거도 확인했습니다." in composed.text
    assert composed.text.count("## 경쟁 구도") == 1
    assert "첫 번째 관찰입니다." in composed.text
    assert "두 번째 관찰입니다." in composed.text


def test_a_all_commentary_precedes_deterministic_coverage_and_tables() -> None:
    rendered = DeterministicRender(
        profile="clinical_portfolio",
        nodes=(
            RenderNode(block_id="clinical:coverage", text="## 조사 범위\n14건"),
            RenderNode(block_id="clinical:table", text="| 시험 | 상태 |\n|---|---|\n| NCT1 | 모집 중 |"),
        ),
    )
    commentary = (
        "질문에 대한 답입니다.\n\n"
        "## 경쟁 구도\n후속 해설입니다.\n\n"
        "## 의미\n마지막 해설입니다."
    )

    composed = compose_lossless_answer(
        rendered,
        commentary,
        synthesis_trace={},
        mode="inject",
    )

    assert composed.text.index("마지막 해설입니다.") < composed.text.index("## 조사 범위")


def test_a_t2_status_and_phase_aggregation_explains_the_distribution() -> None:
    statuses = (
        "RECRUITING",
        "RECRUITING",
        "RECRUITING",
        "RECRUITING",
        "COMPLETED",
        "TERMINATED",
    )
    phases = ("PHASE3", "PHASE3", "PHASE4", "PHASE2", "PHASE2", "PHASE1")
    records = tuple(
        EvidenceRecord(
            evidence_id=f"ct:{index}",
            source="clinicaltrials",
            result_kind="clinical",
            payload={
                "nct_id": f"NCT{index:08d}",
                "overall_status": status,
                "phase": phase,
                "sponsor": "JW중외제약",
            },
        )
        for index, (status, phase) in enumerate(zip(statuses, phases, strict=True), start=1)
    )

    realized = build_narrative_realization(
        (_evidence("clinicaltrials", *records),),
        tuple(record.evidence_id for record in records),
        table_record_ids=tuple(record.evidence_id for record in records),
    )
    surface = " ".join(node.text for node in realized.nodes)

    assert "총 6건 중 모집 중이 4건으로 가장 많" in surface
    assert "후기 단계(3상 이상)는 3건" in surface
    assert "RECRUITING" not in surface
    assert "PHASE3" not in surface


def test_a_cross_source_fusion_binds_three_sentences_to_source_records() -> None:
    evidence_sets = tuple(
        _evidence(
            source,
            EvidenceRecord(
                evidence_id=f"{source}:1",
                source=source,
                result_kind=source,
                payload={
                    "title": f"{source} 자료",
                    "status": "live",
                    "phase": "PHASE3",
                    "sponsor": "JW중외제약",
                    **(
                        {
                            "publisher": "데일리팜",
                            "published_at": "2026-08-13",
                            "summary": "관련 보도",
                        }
                        if source == "web"
                        else {}
                    ),
                },
            ),
        )
        for source in ("patent", "clinicaltrials", "hira", "web")
    )
    record_ids = tuple(
        record.evidence_id for evidence in evidence_sets for record in evidence.records
    )

    realized = build_narrative_realization(evidence_sets, record_ids)
    fusion = next(
        node for node in realized.nodes if node.block_id == "narrative:cross-source-fusion"
    )

    assert len(fusion.text.splitlines()) == 3
    assert set(fusion.record_ids) == set(record_ids)
    assert "데일리팜 · 2026-08-13 · 「web 자료」" in fusion.text
    assert "같은 질문 범위에서 함께 확인됐습니다" not in fusion.text
    assert "patent 자료" in fusion.text
    assert "clinicaltrials 자료" in fusion.text
    assert "상태 게재 중" in fusion.text
    assert "단계 3상" in fusion.text
    assert "원인" not in fusion.text
    assert "때문" not in fusion.text
    assert "한편" not in fusion.text
    assert "각각 확인했습니다" in fusion.text


def test_a_patent_narrative_precedes_auxiliary_mart_commentary() -> None:
    rendered = DeterministicRender(
        profile="patent_portfolio",
        nodes=(
            RenderNode(
                block_id="narrative:field-restatement",
                record_ids=("patent:10-0186853",),
                text=(
                    "10-0186853은 특허구분 물질·용도, "
                    "소멸 사유 존속기간만료로 확인됩니다."
                ),
            ),
            RenderNode(
                block_id="patent:kr-primary",
                record_ids=("patent:10-0186853",),
                text="| 특허번호 |\n|---|\n| 10-0186853 |",
            ),
        ),
    )

    composed = compose_lossless_answer(
        rendered,
        "리바로젯 매출은 100억원입니다.",
        synthesis_trace={},
        mode="inject",
    )

    assert composed.text.index("10-0186853은") < composed.text.index("매출은 100억원")


def test_a_embedded_raw_enum_in_record_title_is_localized() -> None:
    record = EvidenceRecord(
        evidence_id="ct:NCT00548145",
        source="clinicaltrials",
        result_kind="structured_clinical_record",
        payload={
            "title": "P03962 연구(COMPLETED)",
            "overall_status": "COMPLETED",
            "phase": "PHASE3",
            "sponsor": "Merck",
        },
    )

    realized = build_narrative_realization(
        (_evidence("clinicaltrials", record),),
        (record.evidence_id,),
    )
    surface = "\n".join(node.text for node in realized.nodes)

    assert "P03962 연구(완료)" in surface
    assert "COMPLETED" not in surface


def test_a_compose_removes_empty_bold_pseudo_heading() -> None:
    commentary = """**임상 현황**

확인된 임상시험을 설명합니다.

**특허 만료 정보**

**시장 동향**
시장 수치를 설명합니다.
"""

    composed = compose_lossless_answer(
        DeterministicRender(profile="market_analysis"),
        commentary,
        synthesis_trace={},
        mode="inject",
    )

    assert "**임상 현황**" in composed.text
    assert "**특허 만료 정보**" not in composed.text
    assert "**시장 동향**" in composed.text


def test_e_inspection_binds_repeated_clinical_calls_by_nct_id() -> None:
    results = tuple(
        SourceResult(
            source="clinicaltrials",
            query=f"query {nct_id}",
            status="ok",
            payload={
                "calls": [
                    {
                        "render_data": {
                            "request": {"query.intr": ingredient},
                            "payload": {
                                "studies": [
                                    {
                                        "nct_id": nct_id,
                                        "brief_title": title,
                                    }
                                ]
                            },
                        }
                    }
                ]
            },
        )
        for nct_id, ingredient, title in (
            ("NCT00000001", "pitavastatin", "시험 1"),
            ("NCT00000002", "ezetimibe", "시험 2"),
        )
    )
    records = tuple(
        EvidenceRecord(
            evidence_id=f"ct:NCT0000000{index}",
            source="clinicaltrials",
            result_kind="structured_clinical_record",
            payload={
                "nct_id": f"NCT0000000{index}",
                "brief_title": f"시험 {index}",
            },
        )
        for index in (1, 2)
    )
    evidence = _evidence("clinicaltrials", *records)
    rendered = DeterministicRender(
        profile="clinical_portfolio",
        nodes=(
            RenderNode(
                block_id="clinical:records",
                record_ids=tuple(record.evidence_id for record in records),
                text="NCT00000001 NCT00000002",
            ),
        ),
        structured_claims=tuple(
            {"arguments": [{"record_id": record.evidence_id}]}
            for record in records
        ),
    )

    detail = build_inspection_detail(
        _plan("임상 현황"),
        results,
        (evidence,),
        rendered,
        answer_text="NCT00000001 NCT00000002",
    )

    assert [call["counts"] for call in detail["calls"]] == [
        {"returned": 1, "parsed": 1, "envelope": 1, "rendered": 1, "narrated": 1},
        {"returned": 1, "parsed": 1, "envelope": 1, "rendered": 1, "narrated": 1},
    ]


def test_a_relation_insight_is_one_paragraph_bound_to_union_of_t2_records() -> None:
    records = tuple(
        EvidenceRecord(
            evidence_id=f"ct:{index}",
            source="clinicaltrials",
            result_kind="clinical",
            payload={
                "nct_id": f"NCT{index:08d}",
                "overall_status": "RECRUITING" if index < 3 else "COMPLETED",
                "phase": "PHASE3",
                "sponsor": "JW중외제약",
            },
        )
        for index in range(1, 4)
    )
    realized = build_narrative_realization(
        (_evidence("clinicaltrials", *records),),
        tuple(record.evidence_id for record in records),
        table_record_ids=tuple(record.evidence_id for record in records),
    )
    relation = next(
        node for node in realized.nodes if node.block_id == "narrative:cross-record-relations"
    )
    t2_ids = {
        record_id
        for claim in realized.claims
        if claim.claim.claim_type == "T2"
        for record_id in claim.recomputation.record_ids
    }

    assert "\n" not in relation.text
    assert set(relation.record_ids) == t2_ids


def test_a_composition_deduplicates_repeated_leading_sentence() -> None:
    rendered = DeterministicRender(
        profile="clinical_portfolio",
        coverage=CoverageLedger(records_received=1, records_unique=1, records_rendered=1),
        nodes=(RenderNode(block_id="clinical:detail", record_ids=("ct:1",), text="상세 사실"),),
    )
    repeated = "리바로젯 관련 임상시험이 확인됐습니다."
    commentary = (
        f"{repeated} 이어서 핵심 내용을 설명합니다.\n\n"
        f"## 경쟁 구도\n{repeated} 경쟁 구도를 설명합니다."
    )

    composed = compose_lossless_answer(
        rendered,
        commentary,
        synthesis_trace={},
        mode="inject",
    )

    assert composed.text.count(repeated) == 1


def test_a_sentence_deduplication_preserves_dotted_source_name() -> None:
    rendered = DeterministicRender(
        profile="clinical_portfolio",
        nodes=(
            RenderNode(
                block_id="clinical:detail",
                record_ids=("ct:1",),
                text="ClinicalTrials.gov에서 임상시험 14건을 확인했습니다.",
            ),
        ),
    )

    composed = compose_lossless_answer(
        rendered,
        "ClinicalTrials.gov에서 임상시험 14건을 확인했습니다.",
        synthesis_trace={},
        mode="inject",
    )

    assert "ClinicalTrials.gov" in composed.text
    assert "gov에서 임상시험" not in composed.text.replace("ClinicalTrials.gov에서", "")
    assert composed.text.count("임상시험 14건을 확인했습니다.") == 1


def test_a_lossless_synthesis_uses_fact_surface_instead_of_duplicate_raw_payload() -> None:
    payload = {
        "calls": [
            {
                "render_data": {
                    "items": [
                        {
                            "nct_id": "NCT00000001",
                            "brief_summary": "가" * 500_000,
                        }
                    ]
                }
            }
        ]
    }
    result = SourceResult(
        source="clinicaltrials",
        query="리바로젯 임상현황",
        status="ok",
        payload=payload,
    )

    messages = _synthesis_messages(
        _plan("리바로젯 임상현황"),
        (result,),
        (),
        deterministic_facts="NCT00000001은 모집 중이며 3상입니다.",
    )
    prompt = json.loads(messages[-1]["content"])

    assert len(messages[-1]["content"]) < 20_000
    assert prompt["deterministic_facts"].startswith("NCT00000001")
    assert prompt["external_evidence"][0]["detail"] == {
        "omitted": "deterministic_facts contains the rendered evidence"
    }


def test_b_clinical_table_localizes_status_and_phase_enums() -> None:
    record = EvidenceRecord(
        evidence_id="ct:NCT00000001",
        source="clinicaltrials",
        result_kind="structured_clinical_record",
        payload={
            "nct_id": "NCT00000001",
            "brief_title": "시험",
            "overall_status": "RECRUITING",
            "phases": ["PHASE3"],
            "sponsor": "JW중외제약",
        },
    )

    nodes, _required = render_clinical(_evidence("clinicaltrials", record), single=True)
    surface = "\n".join(node.text for node in nodes)

    assert "모집 중" in surface
    assert "3상" in surface
    assert "RECRUITING" not in surface
    assert "PHASE3" not in surface


def test_b_clinical_table_localizes_phase_na() -> None:
    record = EvidenceRecord(
        evidence_id="ct:NCT00000002",
        source="clinicaltrials",
        result_kind="structured_clinical_record",
        payload={
            "nct_id": "NCT00000002",
            "brief_title": "관찰 연구",
            "overall_status": "COMPLETED",
            "phases": ["PHASE_NA"],
            "sponsor": "JW중외제약",
        },
    )

    nodes, _required = render_clinical(_evidence("clinicaltrials", record), single=True)
    surface = "\n".join(node.text for node in nodes)

    assert "해당 없음" in surface
    assert "PHASE_NA" not in surface


def test_b_clinical_detail_localizes_enrollment_type_and_sex_enums() -> None:
    record = EvidenceRecord(
        evidence_id="ct:NCT00000003",
        source="clinicaltrials",
        result_kind="structured_clinical_record",
        payload={
            "nct_id": "NCT00000003",
            "brief_title": "시험",
            "overall_status": "COMPLETED",
            "enrollment": {"count": 120, "type": "ACTUAL"},
            "sex": "ALL",
        },
    )

    nodes, _required = render_clinical(_evidence("clinicaltrials", record), single=True)
    surface = "\n".join(node.text for node in nodes)

    assert "대상자수: 120명 (실제)" in surface
    assert "대상 성별: 전체" in surface
    assert "ACTUAL" not in surface
    assert "ALL" not in surface


@pytest.mark.parametrize(
    ("question", "requested_axis"),
    (
        ("2024년 D693 성별 연령5세구간별 내원일수", "성별·연령5세구간별"),
        ("2024년 D693 진료년월 기준 월별 환자수 추이", "진료년월별"),
    ),
)
def test_c_unsupported_hira_axis_falls_back_to_2024_inpatient_outpatient(
    question: str,
    requested_axis: str,
) -> None:
    route = v4_adapters._hira_stat_route(question)

    assert route.tool == "hira_disease_hospitalization_outpatient_stats"
    assert route.label == "입원/외래"
    assert route.requested_label == requested_axis
    assert route.scope_notice is not None
    assert requested_axis in route.scope_notice
    assert "입원/외래 기준 데이터로 답변합니다" in route.scope_notice


def test_c_hira_fallback_notice_uses_requested_year(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    class Resolver:
        def resolve(self, _query: str, *, allow_default: bool) -> object:
            assert allow_default is False
            raise LookupError

    class External:
        timeout_s = 12

        def hira_disease_name_code(self, code: str) -> ExternalCall:
            return ExternalCall(
                tool="hira_disease_name_code",
                source="HIRA",
                status="live",
                summary_text=code,
                render_data={"items": [{"sick_cd": code}]},
            )

        def hira_disease_hospitalization_outpatient_stats(
            self,
            code: str,
            year: str,
        ) -> ExternalCall:
            calls.append((code, year))
            return ExternalCall(
                tool="hira_disease_hospitalization_outpatient_stats",
                source="HIRA",
                status="live",
                summary_text="one row",
                render_data={
                    "items": [
                        {"sick_cd": code, "year": year, "inpatient_count": 10}
                    ]
                },
            )

    from jw_chat_agent_poc.agent_loop import factory
    from jw_chat_agent_poc.service import general_view_routing

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

    result = v4_adapters.build_source_adapters()["hira"](
        "2023년 D693 진료년월 기준 월별 환자수 추이"
    )

    assert calls == [("D693", "2023")]
    assert result.status == "ok"
    assert result.notice == (
        "요청하신 진료년월별 집계는 현재 지원되지 않아, "
        "입원/외래 기준 2023년 데이터로 답변합니다."
    )
    assert result.payload["period_coverage"]["requested_axis"] == "진료년월별"
    assert result.payload["period_coverage"]["actual_axis"] == "입원/외래"


def test_d_disease_code_web_result_requires_medical_domain_context() -> None:
    decision = filter_web_results(
        "24년 D693 성별 연령5세구간별 내원일수",
        (
            {
                "title": "D693 타이어 규격 안내",
                "snippet": "자동차 타이어 제품 규격과 가격",
                "url": "https://example.com/tire",
            },
            {
                "title": "D693 상병 환자 진료 통계",
                "snippet": "질병 환자의 입원 외래 내원일수",
                "url": "https://example.com/medical",
            },
        ),
    )

    assert [rank for rank, _item in decision.accepted] == [2]
    assert len(decision.exclusions) == 1
    assert decision.exclusions[0].reason_code == "web_medical_domain_not_matched"


def test_a_short_narrative_for_five_records_records_explicit_reason() -> None:
    rendered = DeterministicRender(
        profile="clinical_portfolio",
        coverage=CoverageLedger(records_received=5, records_unique=5, records_rendered=5),
        nodes=(
            RenderNode(
                block_id="narrative:field-restatement",
                record_ids=tuple(f"ct:{index}" for index in range(5)),
                text="짧은 결정론 서술입니다.",
            ),
        ),
    )

    composed = compose_lossless_answer(
        rendered,
        "질문에 대한 짧은 답입니다.",
        synthesis_trace={},
        mode="inject",
    )

    assert composed.trace["narrative_minimum_required"] is True
    assert composed.trace["narrative_character_count"] < 1500
    assert composed.trace["narrative_shortfall_reason"] == "validated prose below 1500 characters"


def test_a_every_compacted_patent_record_remains_in_narrative() -> None:
    records = tuple(
        EvidenceRecord(
            evidence_id=f"patent:KR:{patent_number}",
            source="patent",
            result_kind="structured_patent_record",
            payload={
                "patent_number": patent_number,
                "status": "소멸",
                "patent_type": "제품특허",
                "extinction_reason": reason,
            },
        )
        for patent_number, reason in (
            ("10-0186853", "존속기간만료"),
            ("10-0596257", "등록료불납"),
            ("10-1244508", "무효"),
            ("10-1198822", "존속기간만료"),
        )
    )
    realized = build_narrative_realization(
        (_evidence("patent", *records),),
        tuple(record.evidence_id for record in records),
        table_record_ids=tuple(record.evidence_id for record in records),
    )

    micro = next(node for node in realized.nodes if node.block_id == "narrative:field-restatement")

    assert set(micro.record_ids) == {record.evidence_id for record in records}
    assert all(record.payload["patent_number"] in micro.text for record in records)
    assert realized.unnarrated_record_count == 0
    assert any(
        claim.claim.claim_type == "T2" and claim.claim.operator_id == "GROUP_COUNT"
        for claim in realized.claims
    )


def test_a_sparse_record_with_public_identifier_is_narrated() -> None:
    record = EvidenceRecord(
        evidence_id="web:1",
        source="web",
        result_kind="web_record",
        payload={
            "title": "리바로젯 제네릭 도전",
            "publisher": "데일리팜",
        },
    )

    realized = build_narrative_realization(
        (_evidence("web", record),),
        (record.evidence_id,),
    )

    micro = next(node for node in realized.nodes if node.block_id == "narrative:field-restatement")
    assert micro.record_ids == (record.evidence_id,)
    assert "리바로젯 제네릭 도전" in micro.text
    assert realized.unnarrated_record_count == 0


def test_a_narrative_character_floor_scales_with_every_rendered_record() -> None:
    records = tuple(
        EvidenceRecord(
            evidence_id=f"ct:NCT{index:08d}",
            source="clinicaltrials",
            result_kind="structured_clinical_record",
            payload={
                "nct_id": f"NCT{index:08d}",
                "overall_status": "RECRUITING",
                "phases": ("PHASE3",),
                "sponsor": f"Sponsor {index}",
                "start_date": f"2026-01-{index:02d}",
            },
        )
        for index in range(1, 21)
    )
    realized = build_narrative_realization(
        (_evidence("clinicaltrials", *records),),
        tuple(record.evidence_id for record in records),
        table_record_ids=tuple(record.evidence_id for record in records),
    )

    micro = next(node for node in realized.nodes if node.block_id == "narrative:field-restatement")
    prose_count = len("".join(line.strip() for line in micro.text.splitlines()))

    assert len(micro.record_ids) == len(records)
    assert all(record.payload["nct_id"] in micro.text for record in records)
    assert prose_count >= max(1500, len(records) * 80)


def test_a_narrative_uses_loaded_source_fields_and_reports_usage_metrics() -> None:
    records = (
        EvidenceRecord(
            evidence_id="ct:NCT05151731",
            source="clinicaltrials",
            result_kind="structured_clinical_record",
            payload={
                "nct_id": "NCT05151731",
                "brief_title": "진행성 고형암 시험",
                "conditions": ["고형암"],
                "interventions": ["시험약 A"],
                "phases": ["PHASE2"],
                "overall_status": "RECRUITING",
                "sponsor": "JW중외제약",
                "enrollment": {"count": 120, "type": "ACTUAL"},
                "start_date": "2025-06-25",
                "completion_date": "2027-03-31",
                "primary_outcomes": [
                    {"measure": "객관적 반응률", "time_frame": "24개월"}
                ],
                "facilities": ["서울대병원", "세브란스병원"],
                "countries": ["대한민국"],
                "collaborators": ["국립암센터"],
                "eligibility_criteria": "만 19세 이상이며 측정 가능한 병변이 있는 환자",
                "brief_summary": "시험 목적과 설계를 설명하는 요약입니다.",
            },
        ),
        EvidenceRecord(
            evidence_id="patent:KR:10-0186853",
            source="patent",
            result_kind="structured_patent_record",
            payload={
                "patent_no": "10-0186853",
                "invention_title": "피타바스타틴 복합 조성물",
                "patent_type": "조성물",
                "status": "소멸",
                "extinction_reason": "존속기간만료",
                "expiration_date": "2026-01-09",
                "owner": "JW중외제약",
                "pms_end_date": "2024-08-01",
            },
        ),
        EvidenceRecord(
            evidence_id="hira:notice:2026-101",
            source="hira",
            result_kind="policy_document",
            payload={
                "notice_number": "고시 제2026-101호",
                "title": "리바로젯 급여기준",
                "effective_date": "2026-08-01",
                "target_product": "리바로젯정",
                "active_ingredient": "피타바스타틴·에제티미브",
            },
        ),
        EvidenceRecord(
            evidence_id="mart:리바로젯:2026",
            source="mart",
            result_kind="mart",
            payload={
                "brand": "리바로젯",
                "period": "2026",
                "sales": 124.54,
                "unit": "억원",
                "delta_krw": 24.54,
                "market_share": 12.3,
            },
        ),
    )

    realized = build_narrative_realization(
        (
            _evidence("clinicaltrials", records[0]),
            _evidence("patent", records[1]),
            _evidence("hira", records[2]),
            _evidence("mart", records[3]),
        ),
        tuple(record.evidence_id for record in records),
    )
    surface = "\n".join(node.text for node in realized.nodes)

    for expected in (
        "진행성 고형암 시험",
        "객관적 반응률",
        "120명",
        "서울대병원; 세브란스병원",
        "국립암센터",
        "만 19세 이상",
        "피타바스타틴 복합 조성물",
        "고시 제2026-101호",
        "2026-08-01",
        "124.54",
        "24.54",
        "12.3",
    ):
        assert expected in surface
    assert realized.identifier_only_sentence_count == 0
    assert realized.average_narrated_field_count >= 3
    assert realized.loaded_field_narrative_use_rate == 1.0
    assert all(item["used_field_count"] >= 3 for item in realized.record_field_usage)


def test_b_web_items_survive_as_records_with_publication_fields() -> None:
    result = SourceResult(
        source="web",
        query="리바로젯 제네릭",
        status="ok",
        payload={
            "calls": [
                {
                    "tool": "web_search",
                    "status": "live",
                    "render_data": {
                        "items": [
                            {
                                "title": "리바로젯 제네릭 도전",
                                "url": "https://www.dailypharm.com/news/1",
                                "snippet": "제네릭 경쟁 현황을 정리한 기사입니다.",
                                "published_at": "2026-08-13",
                            }
                        ]
                    },
                }
            ]
        },
    )

    evidence = build_evidence_sets(_plan(), (result,), observed_on=date(2026, 8, 14))[0]
    record = evidence.records[0]

    assert evidence.coverage.records_received == 1
    assert record.payload["title"] == "리바로젯 제네릭 도전"
    assert record.payload["publisher"] == "dailypharm.com"
    assert record.payload["published_at"] == "2026-08-13"
    assert record.payload["summary"] == "제네릭 경쟁 현황을 정리한 기사입니다."


def test_a_final_surface_metrics_ignore_identifiers_that_only_survive_in_tables() -> None:
    records = (
        EvidenceRecord(
            evidence_id="patent:10-0186853",
            source="patent",
            result_kind="structured_patent_record",
            payload={
                "patent_no": "10-0186853",
                "invention_title": "피타바스타틴 복합 조성물",
                "patent_type": "조성물",
                "status": "소멸",
            },
        ),
        EvidenceRecord(
            evidence_id="patent:10-1244508",
            source="patent",
            result_kind="structured_patent_record",
            payload={
                "patent_no": "10-1244508",
                "invention_title": "고지혈증 치료제",
                "patent_type": "용도",
                "status": "소멸(무효)",
            },
        ),
    )
    evidence_sets = (_evidence("patent", *records),)
    realized = build_narrative_realization(
        evidence_sets,
        tuple(record.evidence_id for record in records),
    )
    first_line = realized.nodes[0].text.splitlines()[0]
    final_answer = (
        f"{first_line}\n\n"
        "| 특허번호 | 발명명 |\n"
        "| --- | --- |\n"
        "| 10-0186853 | 피타바스타틴 복합 조성물 |\n"
        "| 10-1244508 | 고지혈증 치료제 |"
    )

    metrics = measure_final_narrative_surface(
        final_answer,
        evidence_sets,
        realized.record_field_usage,
    )

    assert metrics["narrated_record_count"] == 1
    assert metrics["narrated_record_ids"] == ["patent:10-0186853"]
    assert metrics["unnarrated_record_count"] == 1
    assert metrics["narrative_identifier_parity"] is False
    assert metrics["average_narrated_field_count"] == 3.0
    assert metrics["loaded_field_narrative_use_rate"] == 0.5
    assert metrics["identifier_only_sentence_count"] == 0


def test_b_openfda_results_survive_with_public_alias_fields() -> None:
    result = SourceResult(
        source="openfda",
        query="pitavastatin",
        status="ok",
        payload={
            "calls": [
                {
                    "tool": "openfda_label_search",
                    "status": "live",
                    "render_data": {
                        "payload": {
                            "results": [
                                {
                                    "openfda": {
                                        "brand_name": ["LIVALO"],
                                        "substance_name": ["PITAVASTATIN CALCIUM"],
                                    },
                                    "effective_time": "20260131",
                                    "indications_and_usage": ["고콜레스테롤혈증 치료"],
                                }
                            ]
                        }
                    },
                }
            ]
        },
    )

    evidence = build_evidence_sets(_plan(), (result,), observed_on=date(2026, 8, 14))[0]
    record = evidence.records[0]

    assert record.payload["product_name"] == "LIVALO"
    assert record.payload["active_ingredient"] == "PITAVASTATIN CALCIUM"
    assert record.payload["approval_date"] == "20260131"
    assert record.payload["label_section"] == "고콜레스테롤혈증 치료"


def test_b_openfda_public_fields_are_available_to_narrative() -> None:
    record = EvidenceRecord(
        evidence_id="openfda:1",
        source="openfda",
        result_kind="openfda_record",
        payload={
            "product_name": "LIVALO",
            "active_ingredient": "PITAVASTATIN CALCIUM",
            "approval_date": "20260131",
            "label_section": "고콜레스테롤혈증 치료",
        },
    )

    realized = build_narrative_realization(
        (_evidence("openfda", record),),
        (record.evidence_id,),
    )

    assert len(realized.claims) == 1
    assert "성분 PITAVASTATIN CALCIUM" in realized.claims[0].text
    assert "승인일 20260131" in realized.claims[0].text
    assert "라벨 정보 고콜레스테롤혈증 치료" in realized.claims[0].text


def test_b_clinical_long_summary_is_bounded_with_source_presence_marker() -> None:
    record = EvidenceRecord(
        evidence_id="ct:NCT00000001",
        source="clinicaltrials",
        result_kind="structured_clinical_record",
        payload={
            "nct_id": "NCT00000001",
            "brief_title": "시험",
            "overall_status": "RECRUITING",
            "phases": ["PHASE3"],
            "sponsor": "JW중외제약",
            "last_update_date": "2026-08-14",
            "brief_summary": "가" * 1600,
        },
    )

    nodes, _required = render_clinical(_evidence("clinicaltrials", record), single=True)
    detail = next(node for node in nodes if node.block_id == "clinical:record-details")

    assert len(detail.text) < 1600
    assert "[원문 있음]" in detail.text
    assert "원천 미제공" not in detail.text


def test_b_mart_is_available_for_inspection_without_changing_market_surface() -> None:
    result = SourceResult(
        source="mart",
        query="리바로 매출",
        status="ok",
        payload={"calls": [{"tool": "market", "rows": [{"brand": "리바로", "value": 1}]}]},
    )

    evidence_sets = build_evidence_sets(_plan(), (result,), observed_on=date(2026, 8, 14))

    assert len(evidence_sets) == 1
    assert evidence_sets[0].source == "mart"
    assert evidence_sets[0].records[0].payload["brand"] == "리바로"


@pytest.mark.parametrize(
    ("call", "expected_sales"),
    (
        (
            {
                "tool": "get_market_landscape",
                "render_data": {
                    "anchor_brand": "리바로젯",
                    "period": "2026-06",
                    "brand_sales_krw": 12_453_782_153.7,
                    "level_segments": [
                        {"brand": "리바로젯", "ms_recent_pct": 5.3951}
                    ],
                },
            },
            12_453_782_153.7,
        ),
        (
            {
                "tool": "entity_bundle",
                "entity_bundle": {
                    "anchor": "리바로젯",
                    "members": [
                        {
                            "brand": "리바로젯",
                            "role": "target",
                            "render_data": {
                                "period": "2026-06",
                                "value": 12_453_782_153.7,
                                "ms_recent_pct": 5.3951,
                            },
                        }
                    ],
                },
            },
            12_453_782_153.7,
        ),
        (
            {
                "tool": "cause_card_data",
                "render_data": {
                    "ei_ms": {
                        "brand": "리바로젯",
                        "period": "2026-06",
                        "value": 12_453_782_153.7,
                        "ms_recent_pct": 5.3951,
                    }
                },
            },
            12_453_782_153.7,
        ),
    ),
)
def test_a_mart_wrappers_promote_public_identity_and_metrics(
    call: dict[str, object],
    expected_sales: float,
) -> None:
    result = SourceResult(
        source="mart",
        query="리바로젯 특허현황",
        status="ok",
        payload={"calls": [call]},
    )

    (evidence_set,) = build_evidence_sets(
        _plan("리바로젯 특허현황"),
        (result,),
        observed_on=date(2026, 8, 14),
    )
    record = evidence_set.records[0]

    assert record.payload["brand"] == "리바로젯"
    assert record.payload["period"] == "2026-06"
    assert record.payload["sales_krw"] == expected_sales
    assert record.payload["market_share"] == 5.3951


def test_a_nedrug_uppercase_records_are_narratable_with_public_fields() -> None:
    result = SourceResult(
        source="nedrug",
        query="리바로젯정",
        status="ok",
        payload={
            "calls": [
                {
                    "tool": "mfds_permission_search",
                    "status": "live",
                    "render_data": {
                        "items": [
                            {
                                "ITEM_SEQ": "202105578",
                                "ITEM_NAME": "리바로젯정2/10밀리그램",
                                "ENTP_NAME": "제이더블유중외제약(주)",
                                "ITEM_PERMIT_DATE": "20210728",
                                "ITEM_INGR_NAME": (
                                    "Ezetimibe/Pitavastatin Calcium Hydrate"
                                ),
                                "CANCEL_NAME": "정상",
                            }
                        ]
                    },
                }
            ]
        },
    )

    (evidence_set,) = build_evidence_sets(
        _plan("리바로젯 특허현황"),
        (result,),
        observed_on=date(2026, 8, 14),
    )
    record = evidence_set.records[0]
    realized = build_narrative_realization(
        (evidence_set,),
        (record.evidence_id,),
    )

    assert record.payload["item_name"] == "리바로젯정2/10밀리그램"
    assert record.payload["company"] == "제이더블유중외제약(주)"
    assert record.payload["approval_date"] == "20210728"
    assert record.payload["active_ingredient"] == (
        "Ezetimibe/Pitavastatin Calcium Hydrate"
    )
    assert record.payload["status"] == "정상"
    assert realized.unnarrated_record_count == 0
    assert realized.average_narrated_field_count == 4.0
    assert realized.identifier_only_sentence_count == 0
