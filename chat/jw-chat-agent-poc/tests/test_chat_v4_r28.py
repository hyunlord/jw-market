from __future__ import annotations

from datetime import datetime, timezone
import json

from jw_chat_agent_poc.service.v4.contracts import (
    Citation,
    EvidenceEnvelope,
    PlannerOutput,
    RequestedAnswerShape,
    SourceResult,
    ToolQueries,
)
from jw_chat_agent_poc.service.v4.lossless_contracts import (
    CoverageLedger,
    DeterministicRender,
    EvidenceRecord,
    EvidenceSet,
    SourceReference,
)
from jw_chat_agent_poc.service.v4.lossless_spine import compose_lossless_answer
from jw_chat_agent_poc.service.v4.semantic_realization import (
    SemanticEvidenceContext,
    realize_semantic_surface,
)
from jw_chat_agent_poc.service.v4.source_tiers import entity_completion_rows
from jw_chat_agent_poc.service.v4.surface_binding import sanitize_bound_surface
from jw_chat_agent_poc.service.v4.synthesizer import _synthesis_messages


def _queries() -> ToolQueries:
    return ToolQueries(
        mart=("q",),
        nedrug=("q",),
        hira=("q",),
        openfda=("q",),
        clinicaltrials=("q",),
        web=("q",),
        patent=("q",),
    )


def _plan(*, entities: tuple[str, ...] = ()) -> PlannerOutput:
    return PlannerOutput(
        resolved_question="당뇨병 환자수 알려줘",
        expanded_intents=("환자수",),
        answer_sources=("hira",),
        tool_queries=_queries(),
        linking_plan="direct",
        requested_answer_shape=RequestedAnswerShape(entities=entities),
    )


def _hira_context(*, enabled: bool = True) -> SemanticEvidenceContext:
    return SemanticEvidenceContext(
        has_temporal_support=False,
        supported_text="E10 E11 외래 실인원 입원 실인원",
        observed_count=5,
        requested_count=5,
        has_hira_patient_count=enabled,
        hira_code_count=5 if enabled else 0,
    )


def test_hira_semantic_gate_blocks_unsupported_interpretations_but_keeps_observation() -> None:
    answer = "\n".join(
        (
            "남성 환자가 더 많아 질환 발생 위험의 차이를 시사합니다.",
            "외래 3,712,401명과 입원 66,021명은 주로 외래로 만성 관리됨을 명확히 보여줍니다.",
            "E11 외래 실인원이 E10보다 큽니다.",
        )
    )

    realized = realize_semantic_surface(answer, _hira_context())

    assert (
        "남성 환자가 더 많아 질환 발생 위험의 차이를 시사합니다."
        not in realized.text
    )
    assert "발생 위험이나 유병률을 판단하지 않습니다" in realized.text
    assert (
        "외래 3,712,401명과 입원 66,021명은 주로 외래로 만성 관리됨을 명확히 보여줍니다."
        not in realized.text
    )
    assert "진료 방식이나 만성 관리 여부를 판단하지 않습니다" in realized.text
    assert "E11 외래 실인원이 E10보다 큽니다." in realized.text
    assert "중복 제거 여부가 확인되지 않아 합산한 총계는 제시하지 않습니다" in realized.text
    assert {item["from"] for item in realized.transformations} >= {
        "HIRA_RATE_OR_RISK",
        "HIRA_CARE_PATHWAY_INTERPRETATION",
        "HIRA_CODE_SUM",
    }


def test_hira_semantic_gate_failure_injection_disabled_restores_bad_claims() -> None:
    bad = "외래 실인원이 입원보다 많아 주로 외래로 만성 관리됨을 보여줍니다."

    disabled = realize_semantic_surface(bad, _hira_context(enabled=False))
    enabled = realize_semantic_surface(bad, _hira_context(enabled=True))

    assert bad in disabled.text
    assert bad not in enabled.text


def test_hira_semantic_gate_blocks_table_cells_and_mixed_limitation_claims() -> None:
    answer = "\n".join(
        (
            "| 구분 | 해석 |",
            "| --- | --- |",
            "| 성별 | 유병률과 다르지만 남성의 발생 위험이 더 높습니다 |",
            "| 진료 | 외래가 입원보다 많아 주로 외래로 만성 관리됨을 보여줍니다 |",
        )
    )

    realized = realize_semantic_surface(answer, _hira_context())

    assert "남성의 발생 위험이 더 높습니다" not in realized.text
    assert "주로 외래로 만성 관리됨을 보여줍니다" not in realized.text
    assert "발생 위험이나 유병률을 판단하지 않습니다" in realized.text
    assert "진료 방식이나 만성 관리 여부를 판단하지 않습니다" in realized.text


def test_hira_semantic_gate_checks_each_rate_claim_not_a_conjunction_allowlist() -> None:
    answer = "\n".join(
        (
            "HIRA 청구 실인원은 유병률과 다르며 남성의 발생 위험이 더 높습니다.",
            "HIRA 청구 실인원은 유병률과 다르고 남성의 발생 위험이 더 높습니다.",
            "| 구분 | 해석 |",
            "| --- | --- |",
            "| 성별 | 유병률과 다르며 남성의 발생 위험이 더 높습니다 |",
        )
    )

    realized = realize_semantic_surface(answer, _hira_context())

    assert "발생 위험이 더 높" not in realized.text
    assert realized.deletion_count == 3


def test_empty_core_is_recovered_without_an_unrelated_line_removal() -> None:
    evidence = EvidenceSet(
        source="hira",
        retrieved_at="2026-08-17T00:00:00Z",
        coverage=CoverageLedger(records_received=1, records_unique=1),
        records=(
            EvidenceRecord(
                evidence_id="hira:E11",
                source="hira",
                result_kind="patient_count",
                payload={"code": "E11", "ptntCnt": 3712401},
            ),
        ),
    )
    answer = "## 핵심 답\n\n## 근거와 맥락\nE11 외래 실인원은 3,712,401명입니다."

    sanitized, trace = sanitize_bound_surface("당뇨병 E11", answer, (evidence,), ())

    assert sanitized.startswith("## 핵심 답\nE11 외래 실인원은 3,712,401명입니다.")
    assert trace["core_section_recovered_from"] == "근거와 맥락"


def test_hira_completion_scope_does_not_call_disease_codes_brands() -> None:
    plan = _plan(entities=("E10", "E11"))
    result = SourceResult(
        source="hira",
        query="E10 환자수",
        status="ok",
        payload={"code": "E10", "ptntCnt": 10},
    )

    completion = entity_completion_rows(plan, (result,))

    assert "상병코드·질환 항목" in completion.scope_notice
    assert "브랜드" not in completion.scope_notice


def test_hira_completion_scope_uses_source_type_when_question_has_no_patient_token() -> None:
    plan = PlannerOutput(
        resolved_question="당뇨와 고혈압 2024년 외래 비교",
        expanded_intents=("외래 비교",),
        answer_sources=("hira",),
        tool_queries=_queries(),
        linking_plan="direct",
        requested_answer_shape=RequestedAnswerShape(entities=("당뇨", "고혈압")),
    )
    result = SourceResult(
        source="hira",
        query="당뇨 외래",
        status="ok",
        payload={"sickNm": "당뇨", "inpatOpat": "외래", "ptntCnt": 10},
    )

    completion = entity_completion_rows(plan, (result,))

    assert "상병코드·질환 항목" in completion.scope_notice
    assert "브랜드" not in completion.scope_notice


def _clinical_result(count: int) -> SourceResult:
    citations = tuple(
        Citation(
            source="clinicaltrials",
            query="diabetes",
            url=f"https://clinicaltrials.gov/study/NCT{i:08d}",
            retrieved_at=datetime(2026, 8, 17, tzinfo=timezone.utc),
        )
        for i in range(count)
    )
    return SourceResult(
        source="clinicaltrials",
        query="diabetes",
        status="ok",
        payload={
            "records": [
                {
                    "nct_id": f"NCT{i:08d}",
                    "url": f"https://clinicaltrials.gov/study/NCT{i:08d}",
                }
                for i in range(count)
            ]
        },
        citations=citations,
        evidence=EvidenceEnvelope(
            kind="clinical",
            entity_match="EXACT",
            source_scope="GLOBAL",
            time_match="NOT_REQUESTED",
            eligible_claims=("study_design",),
            causal=False,
        ),
    )


def test_e2e_prompt_and_composition_do_not_reintroduce_1004_raw_urls(monkeypatch) -> None:
    monkeypatch.setenv("CHAT_V4_SOURCE_RENDER_LIMIT", "40")
    result = _clinical_result(1004)
    messages = _synthesis_messages(_plan(), (result,), ())
    prompt = json.loads(messages[1]["content"])

    detail = prompt["external_evidence"][0]["detail"]
    assert "records" not in detail
    assert len(prompt["source_mapping"]) == 40

    refs = tuple(
        SourceReference(url=f"https://clinicaltrials.gov/study/NCT{i:08d}")
        for i in range(40)
    )
    rendered = DeterministicRender(
        profile="market_analysis",
        request_notice=(
            "clinicaltrials: 40/1004 표시(상류 반환 순서의 앞 항목(임의 선택)), "
            "나머지는 조회 상세에 보존"
        ),
        source_refs=refs,
    )
    model_answer = "## 핵심 답\n환자수 근거입니다.\n\n## 출처\n" + "\n".join(
        f"- https://clinicaltrials.gov/study/NCT{i:08d}" for i in range(1004)
    )

    composed = compose_lossless_answer(
        rendered,
        model_answer,
        synthesis_trace={"status": "ok"},
        mode="inject",
        request_satisfaction_mode="inject",
    )

    assert len(result.citations) == 1004
    assert composed.text.count("https://clinicaltrials.gov/study/") == 40
    assert "40/1004 표시" in composed.text
    assert composed.trace["model_source_lines_ignored"] == 1004


def test_e2e_source_limit_failure_injection_restores_all_links(monkeypatch) -> None:
    result = _clinical_result(1004)
    monkeypatch.setenv("CHAT_V4_SOURCE_RENDER_LIMIT", "1004")
    unbounded = json.loads(_synthesis_messages(_plan(), (result,), ())[1]["content"])
    monkeypatch.setenv("CHAT_V4_SOURCE_RENDER_LIMIT", "40")
    bounded = json.loads(_synthesis_messages(_plan(), (result,), ())[1]["content"])

    assert len(unbounded["source_mapping"]) == 1004
    assert len(bounded["source_mapping"]) == 40
