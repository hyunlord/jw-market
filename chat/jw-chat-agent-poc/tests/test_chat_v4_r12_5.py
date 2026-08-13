from __future__ import annotations

from datetime import date

from jw_chat_agent_poc.service.v4.charts import build_grounded_charts
from jw_chat_agent_poc.service.v4.clinical import normalize_clinical_study
from jw_chat_agent_poc.service.v4.contracts import (
    PlannerOutput,
    RequestedAnswerShape,
    SourceResult,
    ToolQueries,
)
from jw_chat_agent_poc.service.v4.expansion import (
    build_second_hop_expansion,
    expand_parameter_axes,
)
from jw_chat_agent_poc.service.v4.inspection import build_inspection_detail
from jw_chat_agent_poc.service.v4.lossless_contracts import (
    CoverageLedger,
    DeterministicRender,
    EvidenceRecord,
    EvidenceSet,
    RenderNode,
)
from jw_chat_agent_poc.service.v4.render_clinical import render_clinical
from jw_chat_agent_poc.service.v4.narrative_realization import build_narrative_realization
from jw_chat_agent_poc.service.v4.source_tiers import source_tier


def _plan(question: str, *, answer_sources=("hira",)) -> PlannerOutput:
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
            entities=("E10~E14",),
            measure_or_attribute=("patient_count",),
        ),
    )


def test_a_expands_kcd_and_explicit_year_axes_deterministically() -> None:
    question = "2022년과 2024년 E10~E14 환자수 비교"
    first = expand_parameter_axes(_plan(question), question, observed_on=date(2026, 8, 13))
    second = expand_parameter_axes(_plan(question), question, observed_on=date(2026, 8, 13))

    assert first == second
    assert first.trace["axes"] == {
        "kcd_codes": ["E10", "E11", "E12", "E13", "E14"],
        "years": [2022, 2024],
    }
    assert first.plan.tool_queries.hira == tuple(
        f"{code} 환자수 {year}년"
        for code in ("E10", "E11", "E12", "E13", "E14")
        for year in (2022, 2024)
    )


def test_a_second_hop_uses_observed_products_and_records_truncation() -> None:
    plan = _plan("혈우병 치료제 특허현황", answer_sources=("patent",))
    result = SourceResult(
        source="nedrug",
        query="혈우병 치료제",
        status="ok",
        payload={
            "records": [
                {"item_name": "제품가"},
                {"item_name": "제품나"},
                {"item_name": "제품다"},
                {"item_name": "제품라"},
            ]
        },
    )

    expanded = build_second_hop_expansion(plan, plan.resolved_question, (result,), max_queries=3)

    assert expanded is not None
    assert expanded.plan.tool_queries.patent == (
        "제품가 특허현황",
        "제품나 특허현황",
        "제품다 특허현황",
    )
    assert expanded.trace["truncated_count"] == 1
    assert expanded.trace["source"] == "nedrug"


def test_c_web_is_tier_one_when_not_the_primary_answer_source() -> None:
    assert source_tier(_plan("리바로젯 특허현황", answer_sources=("patent",)), "web") == 1


def test_c_clinical_projection_preserves_protocol_detail_fields() -> None:
    normalized = normalize_clinical_study(
        {
            "protocolSection": {
                "identificationModule": {"nctId": "NCT00000001", "briefTitle": "시험"},
                "designModule": {"studyType": "INTERVENTIONAL", "enrollmentInfo": {"count": 42}},
                "armsInterventionsModule": {
                    "interventions": [{"type": "DRUG", "name": "약물A", "description": "10 mg"}]
                },
                "sponsorCollaboratorsModule": {
                    "leadSponsor": {"name": "주관사"},
                    "collaborators": [{"name": "협력사"}],
                },
                "contactsLocationsModule": {
                    "locations": [{"facility": "서울병원", "country": "Korea, Republic of"}]
                },
                "outcomesModule": {
                    "primaryOutcomes": [{"measure": "1차 지표", "timeFrame": "12주"}],
                    "secondaryOutcomes": [{"measure": "2차 지표"}],
                },
                "descriptionModule": {"briefSummary": "간략 요약", "detailedDescription": "상세 설명"},
                "eligibilityModule": {
                    "eligibilityCriteria": "선정 및 제외 기준",
                    "sex": "ALL",
                    "minimumAge": "18 Years",
                    "maximumAge": "75 Years",
                },
            },
            "hasResults": True,
        }
    )

    assert normalized["intervention_details"][0]["description"] == "10 mg"
    assert normalized["collaborators"] == ["협력사"]
    assert normalized["facilities"] == ["서울병원"]
    assert normalized["primary_outcomes"][0]["measure"] == "1차 지표"
    assert normalized["secondary_outcomes"][0]["measure"] == "2차 지표"
    assert normalized["brief_summary"] == "간략 요약"
    assert normalized["eligibility_criteria"] == "선정 및 제외 기준"
    assert normalized["sex"] == "ALL"
    assert normalized["minimum_age"] == "18 Years"


def test_c_clinical_renderer_keeps_all_records_and_adds_record_details() -> None:
    records = tuple(
        EvidenceRecord(
            evidence_id=f"ct:{index}",
            source="clinicaltrials",
            result_kind="clinical",
            payload={
                "nct_id": f"NCT{index:08d}",
                "brief_title": f"시험 {index}",
                "overall_status": "COMPLETED",
                "interventions": ["약물A"],
                "primary_outcomes": [{"measure": f"지표 {index}"}],
                "brief_summary": f"요약 {index}",
            },
        )
        for index in range(1, 24)
    )
    evidence = EvidenceSet(
        source="clinicaltrials",
        query_spec=("Pitavastatin AND Ezetimibe",),
        retrieved_at="2026-08-13T00:00:00Z",
        coverage=CoverageLedger(records_received=23, records_unique=23, records_relevant=23),
        records=records,
    )

    nodes, _required = render_clinical(evidence, single=False)

    rendered = {record_id for node in nodes for record_id in node.record_ids}
    assert rendered == {record.evidence_id for record in records}
    assert all("외 " not in node.text for node in nodes)
    assert any("1차 평가변수" in node.text and "간략 요약" in node.text for node in nodes)


def test_d_chart_values_are_bound_to_the_same_rendered_records() -> None:
    records = tuple(
        EvidenceRecord(
            evidence_id=f"mart:{period}",
            source="mart",
            result_kind="mart",
            payload={"period": period, "brand": "리바로젯", "sales": value, "unit": "억원"},
        )
        for period, value in (("2025", 100.0), ("2026", 124.54))
    )
    evidence = EvidenceSet(
        source="mart",
        retrieved_at="2026-08-13T00:00:00Z",
        coverage=CoverageLedger(records_received=2, records_unique=2, records_rendered=2),
        records=records,
    )

    charts = build_grounded_charts((evidence,), tuple(record.evidence_id for record in records))

    assert charts[0]["chart_type"] == "line"
    assert charts[0]["x"]["values"] == ["2025", "2026"]
    assert charts[0]["series"][0]["values"] == [100.0, 124.54]
    assert charts[0]["series"][0]["record_ids"] == ["mart:2025", "mart:2026"]


def test_f_inspection_uses_backend_counts_and_sanitizes_internal_values() -> None:
    plan = _plan("리바로젯 특허현황", answer_sources=("patent",))
    result = SourceResult(
        source="patent",
        query="리바로젯 특허현황 http://internal.svc/json?api_key=secret",
        status="ok",
        payload={"records": [{"patent_number": "10-1"}, {"patent_number": "10-2"}]},
        elapsed_ms=1200,
    )
    evidence = EvidenceSet(
        source="patent",
        retrieved_at="2026-08-13T00:00:00Z",
        coverage=CoverageLedger(records_received=2, records_unique=2, records_rendered=1),
        records=(
            EvidenceRecord(
                evidence_id="patent:1",
                source="patent",
                result_kind="patent",
                payload={"patent_number": "10-1"},
            ),
            EvidenceRecord(
                evidence_id="patent:2",
                source="patent",
                result_kind="patent",
                payload={"patent_number": "10-2"},
            ),
        ),
    )
    rendered = DeterministicRender(
        profile="patent_portfolio",
        nodes=(RenderNode(block_id="patent:records", record_ids=("patent:1",), text="표"),),
        coverage=evidence.coverage,
        structured_claims=(
            {"arguments": [{"record_id": "patent:1"}]},
        ),
    )

    detail = build_inspection_detail(plan, (result,), (evidence,), rendered)
    call = detail["calls"][0]

    assert call["counts"] == {"returned": 2, "parsed": 2, "rendered": 1, "narrated": 1}
    assert call["unused_count"] == 1
    assert call["status"] == "완료"
    serialized = str(detail)
    assert "internal.svc" not in serialized
    assert "secret" not in serialized


def test_d_derived_metrics_are_recomputed_from_bound_records() -> None:
    records = (
        EvidenceRecord(
            evidence_id="mart:a",
            source="mart",
            result_kind="mart",
            payload={
                "brand": "A",
                "market_share": 20.0,
                "competitive_market_share": 20.0,
                "overall_market_share": 10.0,
                "sales_share": 20.0,
                "volume_share": 10.0,
                "brand_growth": 12.0,
                "market_growth": 5.0,
                "share_change_contribution": 7.0,
            },
        ),
        EvidenceRecord(
            evidence_id="mart:b",
            source="mart",
            result_kind="mart",
            payload={"brand": "B", "market_share": 10.0},
        ),
    )
    evidence = EvidenceSet(
        source="mart",
        retrieved_at="2026-08-13T00:00:00Z",
        coverage=CoverageLedger(records_received=2, records_unique=2, records_rendered=2),
        records=records,
    )

    realization = build_narrative_realization(
        (evidence,), tuple(record.evidence_id for record in records)
    )
    proofs = {item.operator_id: item for item in realization.recomputations}

    assert proofs["CER"].expected == 2.0
    assert proofs["PRICE_MIX_INDEX"].expected == 2.0
    assert proofs["GROWTH_DECOMP"].expected["recomputed_growth"] == 12.0
    assert proofs["CONCENTRATION_CR5"].expected == 30.0
    assert set(proofs["PEER_ZSCORE"].expected) == {"mart:a", "mart:b"}
