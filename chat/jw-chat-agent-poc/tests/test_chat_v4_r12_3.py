from __future__ import annotations

from datetime import date
from jw_chat_agent_poc.service.v4.contracts import (
    PlannerOutput,
    RequestedAnswerShape,
    ToolQueries,
)
from jw_chat_agent_poc.service.v4.deterministic_render import render_deterministic_facts
from jw_chat_agent_poc.service.v4.lossless_contracts import (
    CoverageLedger,
    EvidenceRecord,
    EvidenceSet,
)
from jw_chat_agent_poc.service.v4.narrative_realization import (
    ALLOWED_T2_OPERATORS,
    build_narrative_realization,
    verify_recomputation,
)


def _evidence(*records: EvidenceRecord, source: str = "clinicaltrials") -> EvidenceSet:
    return EvidenceSet(
        source=source,
        retrieved_at="2026-08-13T00:00:00Z",
        coverage=CoverageLedger(
            total_reported=len(records),
            records_received=len(records),
            records_unique=len(records),
        ),
        records=records,
    )


def _record(
    record_id: str,
    *,
    status: str,
    sponsor: str,
    start_date: str,
    enrollment: int,
) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=f"ct:{record_id}",
        source="clinicaltrials",
        result_kind="structured_clinical_record",
        payload={
            "nct_id": record_id,
            "overall_status": status,
            "sponsor": sponsor,
            "start_date": start_date,
            "enrollment": enrollment,
        },
    )


def _plan(question: str) -> PlannerOutput:
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
        answer_sources=("clinicaltrials",),
        tool_queries=ToolQueries(**query_map),
        linking_plan="first hop is sufficient",
        requested_answer_shape=RequestedAnswerShape(),
    )


def test_a_t2_claims_are_bounded_to_allowed_recomputable_operators() -> None:
    evidence = _evidence(
        _record(
            "NCT00000001",
            status="COMPLETED",
            sponsor="Alpha Pharma",
            start_date="2023-01-01",
            enrollment=100,
        ),
        _record(
            "NCT00000002",
            status="COMPLETED",
            sponsor="Alpha Pharma",
            start_date="2024-01-01",
            enrollment=200,
        ),
        _record(
            "NCT00000003",
            status="RECRUITING",
            sponsor="Beta Pharma",
            start_date="2024-01-01",
            enrollment=150,
        ),
    )

    realization = build_narrative_realization(
        (evidence,),
        tuple(record.evidence_id for record in evidence.records),
    )

    t2_claims = tuple(
        item for item in realization.claims if item.claim.claim_type == "T2"
    )
    assert 1 <= len(t2_claims) <= 20
    assert {item.claim.operator_id for item in t2_claims} <= ALLOWED_T2_OPERATORS
    assert all(item.recomputation.matched for item in t2_claims)
    assert all(
        verify_recomputation(item.recomputation, (evidence,)).matched
        for item in t2_claims
    )
    assert all(item.claim.causal_level != "CAUSAL" for item in t2_claims)


def test_a_t2_enumerates_every_supported_field_before_applying_cap() -> None:
    records = tuple(
        record.model_copy(
            update={
                "payload": {
                    **record.payload,
                    "completion_date": completion_date,
                    "patient_count": patient_count,
                }
            }
        )
        for record, completion_date, patient_count in (
            (
                _record(
                    "NCT00000001",
                    status="COMPLETED",
                    sponsor="Alpha Pharma",
                    start_date="2023-01-01",
                    enrollment=100,
                ),
                "2024-01-01",
                1000,
            ),
            (
                _record(
                    "NCT00000002",
                    status="COMPLETED",
                    sponsor="Alpha Pharma",
                    start_date="2024-01-01",
                    enrollment=200,
                ),
                "2025-01-01",
                2000,
            ),
            (
                _record(
                    "NCT00000003",
                    status="RECRUITING",
                    sponsor="Alpha Pharma",
                    start_date="2024-01-01",
                    enrollment=150,
                ),
                "2026-01-01",
                1500,
            ),
        )
    )
    evidence = _evidence(*records)

    realization = build_narrative_realization(
        (evidence,), tuple(record.evidence_id for record in records)
    )

    t2_claims = tuple(
        item for item in realization.claims if item.claim.claim_type == "T2"
    )
    assert {item.claim.operator_id for item in t2_claims} == ALLOWED_T2_OPERATORS
    assert {
        item.recomputation.field_path
        for item in t2_claims
        if item.recomputation.field_path is not None
    } >= {
        "payload.start_date",
        "payload.completion_date",
        "payload.enrollment",
        "payload.patient_count",
    }


def test_a_t2_groups_partial_fields_over_only_the_bound_records() -> None:
    complete = _record(
        "NCT00000001",
        status="COMPLETED",
        sponsor="Alpha Pharma",
        start_date="2023-01-01",
        enrollment=100,
    )
    recruiting = _record(
        "NCT00000002",
        status="RECRUITING",
        sponsor="Beta Pharma",
        start_date="2024-01-01",
        enrollment=200,
    )
    missing_status_record = _record(
        "NCT00000003",
        status="COMPLETED",
        sponsor="Gamma Pharma",
        start_date="2025-01-01",
        enrollment=300,
    )
    missing_status = missing_status_record.model_copy(
        update={
            "payload": {
                key: value
                for key, value in missing_status_record.payload.items()
                if key != "overall_status"
            }
        }
    )
    evidence = _evidence(complete, recruiting, missing_status)

    realization = build_narrative_realization(
        (evidence,), tuple(record.evidence_id for record in evidence.records)
    )

    status_group = next(
        item
        for item in realization.claims
        if item.claim.operator_id == "GROUP_COUNT"
        and item.recomputation.field_path == "payload.overall_status"
    )
    assert status_group.recomputation.record_ids == (
        complete.evidence_id,
        recruiting.evidence_id,
    )
    assert status_group.recomputation.expected == {
        "COMPLETED": 1,
        "RECRUITING": 1,
    }
    assert "상태가 제공된 레코드 기준" in status_group.text
    assert verify_recomputation(status_group.recomputation, (evidence,)).matched


def test_a_recomputation_mismatch_is_rejected_instead_of_downgraded() -> None:
    original = _evidence(
        _record(
            "NCT00000001",
            status="COMPLETED",
            sponsor="Alpha Pharma",
            start_date="2023-01-01",
            enrollment=100,
        ),
        _record(
            "NCT00000002",
            status="COMPLETED",
            sponsor="Alpha Pharma",
            start_date="2024-01-01",
            enrollment=200,
        ),
    )
    realization = build_narrative_realization(
        (original,),
        tuple(record.evidence_id for record in original.records),
    )
    count_claim = next(
        item for item in realization.claims if item.claim.operator_id == "COUNT"
    )
    changed = _evidence(original.records[0])

    verification = verify_recomputation(count_claim.recomputation, (changed,))

    assert verification.matched is False
    assert verification.reason_code == "input_records_changed"


def test_b_deterministic_render_adds_bound_micro_narrative_without_losing_records() -> None:
    evidence = _evidence(
        _record(
            "NCT00000001",
            status="COMPLETED",
            sponsor="Alpha Pharma",
            start_date="2023-01-01",
            enrollment=100,
        ),
        _record(
            "NCT00000002",
            status="RECRUITING",
            sponsor="Beta Pharma",
            start_date="2024-01-01",
            enrollment=200,
        ),
    )

    rendered = render_deterministic_facts(
        _plan("임상 현황"),
        (evidence,),
        observed_on=date(2026, 8, 13),
    )

    narrative_nodes = tuple(
        node for node in rendered.nodes if node.block_id.startswith("narrative:")
    )
    assert narrative_nodes
    assert all(node.record_ids for node in narrative_nodes)
    assert rendered.coverage.records_rendered == 2
    assert "[직접 확인]" in rendered.text
    assert any(
        item["claim_type"] == "T2" for item in rendered.structured_claims
    )


def test_b_micro_narrative_discloses_records_left_to_the_lossless_table() -> None:
    evidence = _evidence(
        *(
            _record(
                f"NCT{index:08d}",
                status="COMPLETED",
                sponsor="Alpha Pharma",
                start_date=f"2024-01-{index:02d}",
                enrollment=100 + index,
            )
            for index in range(1, 11)
        )
    )

    realization = build_narrative_realization(
        (evidence,),
        tuple(record.evidence_id for record in evidence.records),
    )

    assert realization.unnarrated_record_count == 2
    assert "나머지 2건은 아래 정본 표" in realization.nodes[0].text


def test_b_micro_narrative_does_not_reference_a_table_for_unrendered_records() -> None:
    clinical = _evidence(
        _record(
            "NCT00000001",
            status="RECRUITING",
            sponsor="Alpha Pharma",
            start_date="2024-01-01",
            enrollment=100,
        )
    )
    patents = _evidence(
        *(
            EvidenceRecord(
                evidence_id=f"patent:KR:{index}",
                source="patent",
                result_kind="structured_patent_record",
                payload={"status": "소멸", "expiration_date": f"2025-01-{index:02d}"},
            )
            for index in range(1, 10)
        ),
        source="patent",
    )

    realization = build_narrative_realization(
        (clinical, patents),
        tuple(record.evidence_id for item in (clinical, patents) for record in item.records),
        table_record_ids=(clinical.records[0].evidence_id,),
    )

    assert realization.unnarrated_record_count == 2
    assert "아래 정본 표" not in realization.nodes[0].text


def test_a_t2_relation_sentences_are_source_scoped() -> None:
    clinical = _evidence(
        _record(
            "NCT00000001",
            status="RECRUITING",
            sponsor="Alpha Pharma",
            start_date="2024-01-01",
            enrollment=100,
        ),
        _record(
            "NCT00000002",
            status="COMPLETED",
            sponsor="Beta Pharma",
            start_date="2025-01-01",
            enrollment=120,
        ),
    )
    patents = _evidence(
        EvidenceRecord(
            evidence_id="patent:KR:1",
            source="patent",
            result_kind="structured_patent_record",
            payload={"status": "소멸", "expiration_date": "2024-01-01"},
        ),
        EvidenceRecord(
            evidence_id="patent:KR:2",
            source="patent",
            result_kind="structured_patent_record",
            payload={"status": "소멸(무효)", "expiration_date": "2025-01-01"},
        ),
        source="patent",
    )

    realization = build_narrative_realization(
        (clinical, patents),
        tuple(record.evidence_id for item in (clinical, patents) for record in item.records),
    )

    source_by_id = {
        record.evidence_id: item.source
        for item in (clinical, patents)
        for record in item.records
    }
    for item in realization.claims:
        if item.claim.claim_type != "T2":
            continue
        sources = {source_by_id[record_id] for record_id in item.recomputation.record_ids}
        assert len(sources) == 1
        expected_label = {
            "clinicaltrials": "ClinicalTrials.gov",
            "patent": "식품의약품안전처 의약품 특허목록",
        }[next(iter(sources))]
        assert expected_label in item.text
