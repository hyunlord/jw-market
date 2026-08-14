from __future__ import annotations

from datetime import date

from jw_chat_agent_poc.service.v4.contracts import PlannerOutput, SourceResult, ToolQueries
from jw_chat_agent_poc.service.v4.deterministic_render import render_deterministic_facts
from jw_chat_agent_poc.service.v4.inspection import build_inspection_detail
from jw_chat_agent_poc.service.v4.evidence_set_support import record_refs
from jw_chat_agent_poc.service.v4.lossless_contracts import (
    CoverageLedger,
    DeterministicRender,
    EvidenceRecord,
    EvidenceSet,
    RenderNode,
)
from jw_chat_agent_poc.service.v4.lossless_spine import compose_lossless_answer
from jw_chat_agent_poc.service.v4.render_policy import render_policy
from jw_chat_agent_poc.service.v4.render_common import cell, table
from jw_chat_agent_poc.service.v4.render_clinical import render_clinical


def _plan(question: str) -> PlannerOutput:
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


def _set(source: str, *records: EvidenceRecord) -> EvidenceSet:
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


def test_d1_failed_policy_record_does_not_create_user_cards() -> None:
    failed = EvidenceRecord(
        evidence_id="hira:failed",
        source="hira",
        result_kind="policy",
        payload={"status": "error", "raw_text": ""},
    )

    nodes, _required = render_policy(_set("hira", failed))

    surface = "\n".join(node.text for node in nodes)
    assert surface == ""
    assert "조회 실패" not in surface
    assert "자동 분류 실패" not in surface


def test_d1_partial_policy_record_omits_missing_sections_and_empty_raw_card() -> None:
    partial = EvidenceRecord(
        evidence_id="hira:partial",
        source="hira",
        result_kind="policy",
        payload={
            "status": "ok",
            "notice_number": "제2026-1호",
            "title": "급여 기준",
            "raw_text": "",
        },
    )

    nodes, _required = render_policy(_set("hira", partial))

    surface = "\n".join(node.text for node in nodes)
    assert "## 고시 정보" in surface
    assert "제2026-1호" in surface
    assert "## 투여대상" not in surface
    assert "## 제외기준" not in surface
    assert "## 투여 방법 및 횟수" not in surface
    assert "## 개정 사유" not in surface
    assert "## 공식 원문 전문" not in surface
    assert "조회 실패" not in surface
    assert "자동 분류 실패" not in surface


def test_d2_auxiliary_records_do_not_render_internal_body_cards() -> None:
    clinical = EvidenceRecord(
        evidence_id="ct:NCT00000001",
        source="clinicaltrials",
        result_kind="clinical",
        payload={
            "nct_id": "NCT00000001",
            "brief_title": "Pitavastatin Ezetimibe trial",
            "overall_status": "RECRUITING",
        },
    )
    fda = EvidenceRecord(
        evidence_id="openfda:1",
        source="openfda",
        result_kind="openfda",
        payload={"product_name": "LIVALO", "status": "live"},
    )

    rendered = render_deterministic_facts(
        _plan("리바로젯 제네릭 임상현황"),
        (_set("clinicaltrials", clinical), _set("openfda", fda)),
        observed_on=date(2026, 8, 14),
    )

    assert all(not node.block_id.startswith("aux:") for node in rendered.nodes)
    assert "| 출처 | 식별자 | 상태 | 요약 |" not in rendered.text


def test_d2_primary_source_narrative_precedes_synthesis_commentary() -> None:
    rendered = DeterministicRender(
        profile="clinical_portfolio",
        nodes=(
            RenderNode(
                block_id="narrative:field-restatement",
                record_ids=("ct:NCT00000001",),
                text="ClinicalTrials.gov에서 NCT00000001을 확인했습니다.",
            ),
        ),
    )

    composed = compose_lossless_answer(
        rendered,
        "특허와 매출을 먼저 설명합니다.",
        synthesis_trace={},
        mode="inject",
    )

    assert composed.text.startswith("ClinicalTrials.gov에서 NCT00000001")


def test_d2_d4_inspection_contains_bounded_output_and_record_drop_ids() -> None:
    raw_records = [
        {"nct_id": "NCT00000001", "brief_title": "kept"},
        {"nct_id": "NCT00000002", "brief_title": "dropped"},
    ]
    result = SourceResult(
        source="clinicaltrials",
        query="ezetimibe AND pitavastatin",
        status="ok",
        payload={"studies": raw_records},
    )
    records = tuple(
        EvidenceRecord(
            evidence_id=f"ct:{item['nct_id']}",
            source="clinicaltrials",
            result_kind="clinical",
            payload=item,
        )
        for item in raw_records
    )
    evidence = _set("clinicaltrials", *records)
    rendered = DeterministicRender(
        profile="clinical_portfolio",
        nodes=(
            RenderNode(
                block_id="clinical:records",
                record_ids=("ct:NCT00000001",),
                text="NCT00000001",
            ),
        ),
        structured_claims=(
            {"arguments": [{"record_id": "ct:NCT00000001"}]},
        ),
    )

    detail = build_inspection_detail(
        _plan("리바로젯 제네릭 임상현황"),
        (result,),
        (evidence,),
        rendered,
        answer_text="NCT00000001",
    )
    call = detail["calls"][0]

    assert call["output"]["records"] == [
        {"identifiers": ["NCT00000001", "kept"]},
        {"identifiers": ["NCT00000002", "dropped"]},
    ]
    render_drop = next(item for item in call["drop_reasons"] if item["stage"] == "render")
    assert render_drop["count"] == 1
    assert render_drop["record_ids"] == ["NCT00000002"]
    assert render_drop["count"] == len(render_drop["record_ids"])


def test_d4_inspection_names_records_rejected_by_relevance_gate() -> None:
    result = SourceResult(
        source="clinicaltrials",
        query="ezetimibe AND pitavastatin",
        status="ok",
        payload={"studies": [{"nct_id": "NCT00000001"}]},
    )
    evidence = EvidenceSet(
        source="clinicaltrials",
        retrieved_at="2026-08-14T00:00:00Z",
        coverage=CoverageLedger(
            records_received=2,
            records_unique=2,
            records_relevant=1,
            records_excluded_by_relevance=1,
        ),
        records=(
            EvidenceRecord(
                evidence_id="ct:NCT00000001",
                source="clinicaltrials",
                result_kind="clinical",
                payload={"nct_id": "NCT00000001"},
            ),
        ),
        query_manifest=(
            {
                "relevance_exclusions": [
                    {"nct_id": "NCT00000002", "reason": "두 성분 AND 불충족"}
                ]
            },
        ),
    )
    rendered = DeterministicRender(
        profile="clinical_portfolio",
        nodes=(
            RenderNode(
                block_id="clinical:records",
                record_ids=("ct:NCT00000001",),
                text="NCT00000001",
            ),
        ),
    )

    detail = build_inspection_detail(
        _plan("리바로젯 제네릭 임상현황"),
        (result,),
        (evidence,),
        rendered,
        answer_text="NCT00000001",
    )

    relevance = next(
        item
        for item in detail["calls"][0]["drop_reasons"]
        if item["stage"] == "relevance"
    )
    assert relevance == {
        "stage": "relevance",
        "count": 1,
        "reason": "두 성분 AND 불충족",
        "record_ids": ["NCT00000002"],
    }


def test_d4_relevance_drop_count_matches_unique_record_identifiers() -> None:
    result = SourceResult(
        source="clinicaltrials",
        query="ezetimibe AND pitavastatin",
        status="ok",
        payload={"studies": []},
    )
    evidence = EvidenceSet(
        source="clinicaltrials",
        retrieved_at="2026-08-14T00:00:00Z",
        coverage=CoverageLedger(records_excluded_by_relevance=2),
        query_manifest=(
            {
                "relevance_exclusions": [
                    {"nct_id": "NCT00000002", "reason": "두 성분 AND 불충족"},
                    {"nct_id": "NCT00000002", "reason": "두 성분 AND 불충족"},
                ]
            },
        ),
    )

    detail = build_inspection_detail(
        _plan("리바로젯 제네릭 임상현황"),
        (result,),
        (evidence,),
        DeterministicRender(profile="clinical_portfolio"),
    )
    relevance = detail["calls"][0]["drop_reasons"][0]

    assert relevance["count"] == 1
    assert relevance["record_ids"] == ["NCT00000002"]


def test_d1_empty_table_does_not_fabricate_a_result_row() -> None:
    assert table(("항목", "값"), ()) == ""


def test_d3_record_derived_markdown_is_escaped() -> None:
    assert cell("10_19세 *강조* `raw`") == r"10\_19세 \*강조\* \`raw\`"


def test_d2_clinical_table_uses_plain_nct_identifier_not_raw_markdown() -> None:
    record = EvidenceRecord(
        evidence_id="ct:NCT00000001",
        source="clinicaltrials",
        result_kind="clinical",
        payload={
            "nct_id": "NCT00000001",
            "url": "https://clinicaltrials.gov/study/NCT00000001",
            "brief_title": "Trial",
        },
    )

    nodes, _required = render_clinical(_set("clinicaltrials", record), single=True)
    surface = "\n".join(node.text for node in nodes)

    assert "| NCT00000001 |" in surface
    assert "[NCT00000001](" not in surface


def test_d2_source_reference_includes_lane_specific_identity_on_one_line() -> None:
    clinical = record_refs(
        {
            "url": "https://clinicaltrials.gov/study/NCT00000001",
            "nct_id": "NCT00000001",
            "brief_title": "Pitavastatin and ezetimibe trial",
            "overall_status": "RECRUITING",
        }
    )[0]
    patent = record_refs(
        {
            "url": "https://patents.example/10-1234567",
            "patent_no": "10-1234567",
            "invention_title": "Combination formulation",
            "applicant": "Example Pharma",
            "expiry_date": "2028-03-01",
            "status": "등록",
        }
    )[0]
    news = record_refs(
        {
            "url": "https://news.example/article/1",
            "publisher": "데일리팜",
            "title": "리바로젯 제네릭 도전",
            "published_at": "2026-08-12",
        }
    )[0]

    assert clinical.title == "NCT00000001 · Pitavastatin and ezetimibe trial · RECRUITING"
    assert patent.title == "10-1234567 · Combination formulation · Example Pharma · 2028-03-01 · 등록"
    assert news.title == "데일리팜 · 리바로젯 제네릭 도전"


def test_d3_numeric_separator_is_not_mistaken_for_sentence_boundary() -> None:
    rendered = DeterministicRender(
        profile="clinical_portfolio",
        nodes=(
            RenderNode(
                block_id="market:numeric",
                record_ids=("mart:1",),
                text="처방량은 약 9. 000만 Rx이며 점유율은 5. 395%입니다.",
            ),
        ),
    )

    composed = compose_lossless_answer(
        rendered,
        "",
        synthesis_trace={},
        mode="inject",
    )

    assert "9,000만 Rx" in composed.text
    assert "5.395%" in composed.text
    assert "9. 000" not in composed.text


def test_d3_sanitizes_duplicate_market_commentary_without_fact_injection() -> None:
    rendered = DeterministicRender(profile="market_analysis")
    repeated = (
        "요청하신 2026-08 데이터를 조회할 수 없습니다. "
        "해당하는 시계열 데이터가 없습니다. "
        "다른 기간 값으로 대체하지 않습니다."
    )

    composed = compose_lossless_answer(
        rendered,
        f"{repeated}\n\n{repeated} [출처: 내부 데이터마트]",
        synthesis_trace={},
        mode="inject",
    )

    assert composed.text.count("요청하신 2026-08 데이터를 조회할 수 없습니다.") == 1
    assert composed.trace["duplicate_leading_sentences_removed"] >= 1


def test_d3_keeps_shadow_commentary_byte_identical() -> None:
    rendered = DeterministicRender(profile="market_analysis")
    commentary = "같은 문장입니다.\n\n같은 문장입니다."

    composed = compose_lossless_answer(
        rendered,
        commentary,
        synthesis_trace={},
        mode="shadow",
    )

    assert composed.text == commentary
    assert composed.answer_mutated is False
