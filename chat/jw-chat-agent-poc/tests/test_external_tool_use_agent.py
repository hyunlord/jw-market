from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
import json
import threading
import time
from typing import Any

from pydantic import BaseModel
import pytest
import requests

from jw_chat_agent_poc.common.timing import new_timing, stage_event_sink
from jw_chat_agent_poc.orchestrator.markdown_formatting import allowed_numbers
from jw_chat_agent_poc.service.answer_safety import ensure_natural_fact_lead
from jw_chat_agent_poc.service.genos_client import GenosClient
from jw_chat_agent_poc.service.runtime_provenance import _facts_returned
from jw_chat_agent_poc.tool_use.catalog import TOOL_DESCRIPTION_CATALOG
from jw_chat_agent_poc.tool_use.contracts import AgentResult, EvidenceFact, ToolEnvelope
from jw_chat_agent_poc.tool_use.executor import AgentExecutor, _execute_with_timeout
import jw_chat_agent_poc.tool_use.integration as integration_module
from jw_chat_agent_poc.tool_use.integration import _deterministic_tool_choices, run_external_tool_agent
from jw_chat_agent_poc.orchestrator.tool_use_contract import tool_use_evidence_complete, tool_use_requirements
from jw_chat_agent_poc.tool_use.provider import GenosToolChoiceProvider, ToolChoice
from jw_chat_agent_poc.tool_use.registry import ExternalToolRegistry
from jw_chat_agent_poc.tool_use.registry import _external_call_envelope
from jw_chat_agent_poc.tool_use.renderer import render_evidence_answer
from jw_chat_agent_poc.tool_use.specs import ToolSpec
from jw_chat_agent_poc.resolver import BrandResolver
from jw_chat_agent_poc.tools.external import ExternalApiClient
from jw_chat_agent_poc.tools.external import ExternalCall
from jw_chat_agent_poc.tools.external.mcp_client import MCP_FIRST_ATTEMPT_TIMEOUT_S


class _NoInput(BaseModel):
    pass


@dataclass(slots=True)
class _ChoiceSequence:
    choices: Sequence[ToolChoice]
    calls: int = field(default=0, init=False)

    def choose(self, *, user_text: str, messages: list[dict], tools: list[dict]) -> ToolChoice:
        del user_text, messages, tools
        choice = self.choices[self.calls]
        self.calls += 1
        return choice


def _fact() -> EvidenceFact:
    return EvidenceFact(
        fact_id="local_molecule:리바로:1",
        subject="리바로",
        metric="성분",
        value=None,
        unit=None,
        period=None,
        source_name="로컬 시장 DB 성분 정보",
        source_locator="pitavastatin",
        raw_ref=None,
    )


def _result_with_fact(tool: str, fact: EvidenceFact, *, answer: str | None = None) -> AgentResult:
    rendered = render_evidence_answer((fact,))
    return AgentResult(
        status="ok",
        answer=rendered if answer is None else answer,
        tool_calls=(
            {
                "tool": tool,
                "source": "nedrug_mcp" if tool.startswith("mfds_") else "clinicaltrials_mcp",
                "status": "ok",
                "summary_text": "근거 1건 확인",
                "render_data": {
                    "ok": True,
                    "preview": "근거 1건 확인",
                    "evidence": [fact.model_dump(mode="json")],
                    "error_code": None,
                    "error_message": None,
                },
            },
        ),
        sources=(fact.source_name,),
        traces=(),
        fallback_code=None,
    )


@pytest.mark.parametrize(
    ("tool", "fact"),
    (
        (
            "mfds_permission_search",
            EvidenceFact(
                fact_id="mfds_permission_detail:아일리아:NB_DOC_DATA",
                subject="아일리아",
                metric="급여 기준",
                value=None,
                unit=None,
                period=None,
                source_name="식약처 의약품 허가 상세",
                source_locator="아일리아주사 · 신생혈관성 연령관련 황반변성 급여 기준",
                raw_ref="mfds_permission_detail:1:NB_DOC_DATA",
            ),
        ),
        (
            "clinicaltrials_study_details",
            EvidenceFact(
                fact_id="clinicaltrials_study_details:NCT05151731:eligibility",
                subject="NCT05151731",
                metric="선정·제외 기준",
                value=None,
                unit=None,
                period=None,
                source_name="ClinicalTrials.gov 임상시험 상세",
                source_locator="Inclusion Criteria: DME · Exclusion Criteria: prior treatment",
                raw_ref="clinicaltrials_study_details:eligibility",
            ),
        ),
    ),
)
def test_mfds_and_clinicaltrials_facts_are_projected_to_markdown_evidence(
    tool: str,
    fact: EvidenceFact,
) -> None:
    result = _result_with_fact(tool, fact)

    payload = integration_module._agent_result_payload("원문 질문", result)

    assert payload["markdown_response"]["fact_md"] == result.answer
    assert payload["markdown_response"]["evidence"] == [
        {
            "fact_id": fact.fact_id,
            "label": fact.metric,
            "value": fact.source_locator,
            "source": fact.source_name,
            "tool": tool,
            "path": fact.raw_ref,
            "period": "",
            "allowed_numbers": list(allowed_numbers(result.answer)),
            "visible": True,
            "entity": fact.subject,
            "metric": fact.metric,
            "unit": "",
            "source_grade": "AUTHORITATIVE",
            "view": "",
            "operand_fact_ids": [],
        }
    ]
    assert _facts_returned(payload["markdown_response"])["evidence_count"] == 1


def test_external_evidence_projection_rejects_fact_not_rendered_in_fact_markdown() -> None:
    fact = EvidenceFact(
        fact_id="mfds_permission_detail:아일리아:NB_DOC_DATA",
        subject="아일리아",
        metric="급여 기준",
        value=None,
        unit=None,
        period=None,
        source_name="식약처 의약품 허가 상세",
        source_locator="아일리아주사 · 급여 기준",
        raw_ref="mfds_permission_detail:1:NB_DOC_DATA",
    )
    result = _result_with_fact("mfds_permission_search", fact, answer="- 아일리아: 허가 품목 = 아일리아주사")

    payload = integration_module._agent_result_payload("아일리아 급여기준", result)

    assert payload["markdown_response"]["evidence"] == []


@pytest.mark.parametrize(
    "tool",
    (
        "hira_disease_hospitalization_outpatient_stats",
        "get_brand_metric",
    ),
)
def test_external_evidence_projection_does_not_expand_to_hira_or_mart_tools(
    tool: str,
) -> None:
    fact = EvidenceFact(
        fact_id="hira_disease:D693:2024",
        subject="D693",
        metric="환자수",
        value=Decimal("3620"),
        unit="명",
        period="2024",
        source_name="건강보험심사평가원",
        source_locator="공식 통계",
        raw_ref="hira_disease:2024",
    )
    result = _result_with_fact(tool, fact)

    payload = integration_module._agent_result_payload("상병코드 D693 환자수 추이", result)

    assert payload["markdown_response"]["evidence"] == []


def test_clinicaltrials_projection_omits_missing_field_placeholders() -> None:
    actual = EvidenceFact(
        fact_id="clinicaltrials_study_details:NCT05151731:title",
        subject="NCT05151731",
        metric="연구 제목",
        value=None,
        unit=None,
        period=None,
        source_name="ClinicalTrials.gov 임상시험 상세",
        source_locator="DME Study · https://clinicaltrials.gov/study/NCT05151731",
        raw_ref="clinicaltrials_study_details:title",
    )
    missing = EvidenceFact(
        fact_id="clinicaltrials_study_details:NCT05151731:start_date",
        subject="NCT05151731",
        metric="시험 시작일",
        value=None,
        unit=None,
        period=None,
        source_name="ClinicalTrials.gov 임상시험 상세",
        source_locator="ClinicalTrials 상세 응답에서 시험 시작일을 확인할 수 없습니다.",
        raw_ref="clinicaltrials_study_details:start_date",
    )
    result = AgentResult(
        status="ok",
        answer=render_evidence_answer((actual, missing)),
        tool_calls=(
            {
                "tool": "clinicaltrials_study_details",
                "source": "clinicaltrials_mcp",
                "status": "ok",
                "summary_text": "근거 2건 확인",
                "render_data": {
                    "ok": True,
                    "evidence": [
                        actual.model_dump(mode="json"),
                        missing.model_dump(mode="json"),
                    ],
                },
            },
        ),
        sources=("ClinicalTrials.gov 임상시험 상세",),
        traces=(),
        fallback_code=None,
    )

    payload = integration_module._agent_result_payload(
        "NCT05151731 임상 디자인(대상, 평가변수, 기간)을 알려줘",
        result,
    )

    evidence = payload["markdown_response"]["evidence"]
    assert [fact["metric"] for fact in evidence] == ["연구 제목"]
    assert all("확인할 수 없습니다" not in fact["value"] for fact in evidence)


def test_evidence_renderer_uses_text_fact_without_placeholder_or_raw_scalars() -> None:
    # Given: a verified text-valued fact without a numeric value.
    fact = _fact()

    # When: the deterministic renderer builds the answer.
    answer = render_evidence_answer((fact,))

    # Then: the text fact is the visible value and no raw/provider shell leaks.
    assert "성분 = pitavastatin" in answer
    assert "= -" not in answer
    assert "resultCode" not in answer
    assert "totalCount" not in answer


def test_web_evidence_preserves_title_url_and_date() -> None:
    # Given: a live web result has explicit provenance fields.
    call = ExternalCall(
        tool="web_search",
        source="web_search",
        status="live",
        summary_text="one result",
        render_data={
            "items": [
                {
                    "title": "고지혈증 치료 가이드라인",
                    "url": "https://example.test/guideline",
                    "published_date": "2026-07-15",
                }
            ]
        },
    )

    # When: the web call becomes evidence and is rendered deterministically.
    envelope = _external_call_envelope(call, "최신 고지혈증 가이드라인", "웹 검색")
    answer = render_evidence_answer(envelope.evidence)

    # Then: title, URL, and provider-supplied date survive the envelope boundary.
    assert "[고지혈증 치료 가이드라인](https://example.test/guideline)" in answer
    assert "(2026-07-15)" in answer


def test_external_transport_failure_is_reported_as_lookup_failure() -> None:
    # Given: the live gateway failed before any evidence could be returned.
    call = ExternalCall(
        tool="openfda_label_search",
        source="openfda_mcp",
        status="error",
        summary_text="gateway unavailable",
        render_data={"message": "MCP lookup failed"},
    )

    # When: the failed call crosses the public ToolEnvelope boundary.
    envelope = _external_call_envelope(call, "pitavastatin", "라벨/이상반응")

    # Then: transport failure is not misreported as an evidence absence.
    assert envelope.ok is False
    assert envelope.error_code == "ERROR"
    assert envelope.error_message == "외부 도구 조회에 실패했습니다."
    assert "근거를 찾지 못" not in envelope.error_message


def test_agent_executor_stops_before_final_llm_when_evidence_is_complete() -> None:
    # Given: one tool call yields complete evidence.
    provider = _ChoiceSequence(
        (
            ToolChoice("evidence_tool", {}, "call evidence tool", call_id="call-1"),
        )
    )
    spec = ToolSpec(
        name="evidence_tool",
        description="when to use: verified fixture. when NOT to use: unrelated questions.",
        input_model=_NoInput,
        execute=lambda _payload: ToolEnvelope(
            ok=True,
            preview="verified",
            evidence=(_fact(),),
            raw={"resultCode": "00", "totalCount": 1},
            error_code=None,
            error_message=None,
        ),
        timeout_s=1.0,
        tags=("local",),
    )

    # When: the tool-use loop runs.
    result = AgentExecutor(provider=provider).run(user_text="리바로 성분", tools=(spec,))

    # Then: deterministic evidence rendering completes without a final generation call.
    assert result.status == "ok"
    assert result.fallback_code is None
    assert provider.calls == 1
    assert "pitavastatin" in result.answer
    assert "resultCode" not in result.answer


def test_agent_executor_runs_all_forced_tools_before_accepting_complete_evidence() -> None:
    calls: list[str] = []
    provider = _ChoiceSequence((ToolChoice(None, {}, "done", call_id=None),))

    def spec(name: str) -> ToolSpec:
        return ToolSpec(
            name=name,
            description=name,
            input_model=_NoInput,
            execute=lambda _payload: (
                calls.append(name)
                or ToolEnvelope(
                    ok=True,
                    preview=name,
                    evidence=(_fact(),),
                    raw=None,
                    error_code=None,
                    error_message=None,
                )
            ),
            timeout_s=1.0,
            tags=("external",),
        )

    result = AgentExecutor(
        provider=provider,
        best_effort=True,
        forced_choices=(
            ToolChoice("clinical", {}, "required clinical", call_id="forced-1"),
            ToolChoice("permission", {}, "required permission", call_id="forced-2"),
        ),
    ).run(user_text="임상과 허가", tools=(spec("clinical"), spec("permission")))

    assert result.status == "ok"
    assert calls == ["clinical", "permission"]
    assert [call["tool"] for call in result.tool_calls] == ["clinical", "permission"]
    assert provider.calls == 0


def test_authoritative_forced_tool_no_evidence_does_not_consult_planner() -> None:
    provider = _ChoiceSequence((ToolChoice("unrelated", {}, "wrong fallback", call_id="planner-1"),))
    spec = ToolSpec(
        name="mfds_composition",
        description="verified composition contract",
        input_model=_NoInput,
        execute=lambda _payload: ToolEnvelope(
            ok=False,
            preview="no matching product",
            evidence=(),
            raw=None,
            error_code="NO_EVIDENCE",
            error_message="식약처 응답에 제품명과 일치하는 성분 조성 근거가 없습니다.",
        ),
        timeout_s=1.0,
        tags=("external", "mfds"),
    )

    result = AgentExecutor(
        provider=provider,
        best_effort=True,
        forced_choices=(
            ToolChoice("mfds_composition", {}, "required composition", call_id="forced-1"),
        ),
        authoritative_forced_choices=True,
    ).run(user_text="NeDrug: 리바로 성분 조성 알려줘", tools=(spec,))

    assert result.status == "fallback"
    assert result.fallback_code is not None
    assert result.fallback_code.value == "VERIFICATION_FAIL"
    assert "제품명과 일치하는 성분 조성 근거" in result.answer
    assert [call["tool"] for call in result.tool_calls] == ["mfds_composition"]
    assert provider.calls == 0


def test_authoritative_forced_tools_preserve_partial_evidence_without_planner() -> None:
    provider = _ChoiceSequence((ToolChoice("unrelated", {}, "wrong fallback", call_id="planner-1"),))

    def spec(name: str, *, ok: bool) -> ToolSpec:
        return ToolSpec(
            name=name,
            description=name,
            input_model=_NoInput,
            execute=lambda _payload: ToolEnvelope(
                ok=ok,
                preview=name,
                evidence=(_fact(),) if ok else (),
                raw=None,
                error_code=None if ok else "NO_EVIDENCE",
                error_message=None if ok else "확인 가능한 근거가 없습니다.",
            ),
            timeout_s=1.0,
            tags=("external",),
        )

    result = AgentExecutor(
        provider=provider,
        best_effort=True,
        forced_choices=(
            ToolChoice("clinical", {}, "required clinical", call_id="forced-1"),
            ToolChoice("permission", {}, "required permission", call_id="forced-2"),
        ),
        parallel_forced_choices=True,
        authoritative_forced_choices=True,
    ).run(
        user_text="임상과 허가",
        tools=(spec("clinical", ok=True), spec("permission", ok=False)),
    )

    assert result.status == "partial"
    assert result.fallback_code is None
    assert "pitavastatin" in result.answer
    assert [call["tool"] for call in result.tool_calls] == ["clinical", "permission"]
    assert provider.calls == 0


def test_agent_executor_runs_preplanned_independent_tools_in_parallel() -> None:
    provider = _ChoiceSequence((ToolChoice(None, {}, "done", call_id=None),))
    barrier = threading.Barrier(3)
    intervals: dict[str, tuple[float, float]] = {}

    def spec(name: str) -> ToolSpec:
        def execute(_payload: BaseModel) -> ToolEnvelope:
            started = time.perf_counter()
            barrier.wait(timeout=0.5)
            time.sleep(0.05)
            intervals[name] = (started, time.perf_counter())
            return ToolEnvelope(
                ok=True,
                preview=name,
                evidence=(_fact(),),
                raw=None,
                error_code=None,
                error_message=None,
            )

        return ToolSpec(
            name=name,
            description=name,
            input_model=_NoInput,
            execute=execute,
            timeout_s=1.0,
            tags=("external",),
        )

    names = (
        "clinicaltrials_v2_search",
        "mfds_clinical_trial_kr",
        "web_search",
    )
    result = AgentExecutor(
        provider=provider,
        best_effort=True,
        forced_choices=tuple(
            ToolChoice(name, {}, f"required {name}", call_id=f"forced-{index}")
            for index, name in enumerate(names, start=1)
        ),
        parallel_forced_choices=True,
    ).run(user_text="뇌경색 임상·허가 경쟁약물", tools=tuple(spec(name) for name in names))

    assert result.status == "ok"
    assert [call["tool"] for call in result.tool_calls] == list(names)
    assert provider.calls == 0
    assert all(
        left[0] < right[1] and right[0] < left[1]
        for index, left in enumerate(intervals.values())
        for right in list(intervals.values())[index + 1 :]
    )


def test_agent_executor_records_the_external_tool_stage_and_evidence_count() -> None:
    timing = new_timing()
    events: list[dict[str, object]] = []
    provider = _ChoiceSequence((ToolChoice("clinicaltrials_v2_search", {}, "run", call_id="call-1"),))
    spec = ToolSpec(
        name="clinicaltrials_v2_search",
        description="verified clinical fixture",
        input_model=_NoInput,
        execute=lambda _payload: ToolEnvelope(
            ok=True,
            preview="verified",
            evidence=(_fact(),),
            raw=None,
            error_code=None,
            error_message=None,
        ),
        timeout_s=1.0,
        tags=("clinicaltrials_mcp",),
    )

    with stage_event_sink(events.append):
        result = AgentExecutor(provider=provider, timing=timing).run(
            user_text="리바로 임상시험",
            tools=(spec,),
        )

    assert result.status == "ok"
    assert timing["stages"] == [
        {
            "name": "tool:clinicaltrials_v2_search",
            "elapsed_ms": timing["stages"][0]["elapsed_ms"],
            "detail": "리바로 임상시험",
        }
    ]
    assert events[-1]["name"] == "임상 데이터 조회"
    assert events[-1]["status"] == "done"
    assert events[-1]["summary"] == "근거 1건 확인"


def test_exact_clinical_permission_competitor_question_forces_valid_contract_tools() -> None:
    question = "고지혈증 질환(성분)의 임상·허가심사 단계 경쟁약물 현황을 알려줘 ."

    choices = _deterministic_tool_choices(question, BrandResolver())

    assert [choice.name for choice in choices] == [
        "clinicaltrials_v2_search",
        "mfds_clinical_trial_kr",
    ]
    assert all(choice.call_id and choice.call_id.startswith("contract-") for choice in choices)
    assert [choice.name for choice in _deterministic_tool_choices("리바로 임상실험", BrandResolver())] == [
        "clinicaltrials_v2_search"
    ]
    assert [choice.name for choice in _deterministic_tool_choices("마운자로 성분", BrandResolver())] == [
        "local_molecule_lookup"
    ]


def test_unbranded_stroke_clinical_review_preplans_independent_tools() -> None:
    question = "뇌경색 임상·허가 경쟁약물"

    choices = _deterministic_tool_choices(question, BrandResolver())

    assert [(choice.name, choice.arguments) for choice in choices] == [
        (
            "clinicaltrials_v2_search",
            {"query": "cerebral infarction", "query_type": "condition"},
        ),
        (
            "mfds_clinical_trial_kr",
            {"query": "뇌경색", "query_type": "condition"},
        ),
    ]


def test_disease_identity_question_uses_hira_mapping_instead_of_molecule_lookup() -> None:
    choices = _deterministic_tool_choices("리바로 질환", BrandResolver())

    assert choices == (
        ToolChoice(
            "hira_disease_name_code",
            {"sick_cd": "E78", "year": "2024"},
            "contract requires hira_disease_name_code",
            call_id="contract-1",
        ),
    )


def test_exact_nct_question_forces_verified_detail_tool_in_off_mode() -> None:
    choices = _deterministic_tool_choices(
        "NCT05151731 임상 디자인(대상, 평가변수, 기간)을 알려줘",
        BrandResolver(),
    )

    assert [(choice.name, choice.arguments) for choice in choices] == [
        ("clinicaltrials_study_details", {"nct_id": "NCT05151731"}),
    ]


def test_nedrug_composition_forces_contract_backed_tool_in_off_mode() -> None:
    choices = _deterministic_tool_choices(
        "NeDrug: 리바로 성분 조성 알려줘",
        BrandResolver(),
    )

    assert [(choice.name, choice.arguments) for choice in choices] == [
        ("mfds_composition", {"brand": "리바로"}),
    ]


@pytest.mark.parametrize(
    "question",
    (
        "아일리아의 급여기준에 대해서 적응증 별로 설명해줘",
        "NeDrug: 아일리아 제품의 효능 효과, 용병 용량, 사용상 주의사항을 알려줘",
        "Eylea 급여기준 알려줘",
        "Aflibercept 급여기준 알려줘",
    ),
)
def test_nedrug_permission_fields_force_contract_backed_tool_in_off_mode(question: str) -> None:
    choices = _deterministic_tool_choices(question, BrandResolver())

    assert [(choice.name, choice.arguments) for choice in choices] == [
        ("mfds_permission_search", {"brand": "아일리아"}),
    ]


@pytest.mark.parametrize(
    "question",
    (
        "아일리아의 급여기준에 대해서 적응증 별로 설명해줘",
        "Eylea 급여기준 알려줘",
        "Aflibercept 급여기준 알려줘",
        "NeDrug: 아일리아 제품의 효능·효과, 용법·용량, 사용상 주의사항을 알려줘",
        "아일리아의 허가 품목명과 업체명을 공식 허가정보 기준으로 알려줘",
    ),
)
def test_forced_legacy_reimbursement_question_uses_nedrug_without_mart_text(
    monkeypatch,
    question: str,
) -> None:
    external = ExternalApiClient(mode="fixture")

    def permission_search(brand: str) -> ExternalCall:
        assert brand == "아일리아"
        return ExternalCall(
            tool="mfds_permission_search",
            source="external_api",
            status="ok",
            summary_text="아일리아 허가 품목 1건",
            render_data={
                "resultCode": "00",
                "items": [{"ITEM_SEQ": "201306324", "ITEM_NAME": "아일리아주사"}],
            },
        )

    def permission_detail(item_seq: str) -> ExternalCall:
        assert item_seq == "201306324"
        return ExternalCall(
            tool="mfds_permission_detail",
            source="external_api",
            status="ok",
            summary_text="아일리아 허가 상세 1건",
            render_data={
                "resultCode": "00",
                "items": [
                    {
                        "ITEM_SEQ": item_seq,
                        "ITEM_NAME": "아일리아주사",
                        "NB_DOC_DATA": "신생혈관성 연령관련 황반변성 급여 기준",
                    }
                ],
            },
        )

    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "OFF")
    monkeypatch.setenv("CHAT_EXTERNAL_TOOL_FORCE_CONTRACT_CALLS", "true")
    monkeypatch.setattr(external, "mfds_permission_search", permission_search)
    monkeypatch.setattr(external, "mfds_permission_detail", permission_detail)

    payload = run_external_tool_agent(
        question,
        resolver=BrandResolver(),
        external=external,
    )

    assert [call["tool"] for call in payload["tool_calls"]] == ["mfds_permission_search"]
    assert "신생혈관성 연령관련 황반변성 급여 기준" in payload["answer"]
    assert "mart" not in payload["answer"].casefold()
    assert "nhi_type" not in payload["answer"]
    evidence = payload["markdown_response"]["evidence"]
    assert evidence
    assert _facts_returned(payload["markdown_response"])["evidence_count"] == len(evidence)
    assert {fact["source_grade"] for fact in evidence} == {"AUTHORITATIVE"}
    assert {fact["entity"] for fact in evidence} == {"아일리아"}


@pytest.mark.parametrize(
    "question",
    (
        "NCT05151731의 inclusion 및 exclusion Criteria 알려줘",
        "NCT05151731 임상 디자인(대상, 평가변수, 기간)을 알려줘",
    ),
)
def test_nct_detail_questions_project_actual_fields_to_markdown_evidence(
    monkeypatch,
    question: str,
) -> None:
    monkeypatch.setenv("CHAT_TOOL_ROUTING_MODE", "ENFORCE")
    payload = run_external_tool_agent(
        question,
        resolver=BrandResolver(),
        external=ExternalApiClient(mode="fixture"),
        provider=_ChoiceSequence((ToolChoice(None, {}, "unused", call_id=None),)),
    )

    evidence = payload["markdown_response"]["evidence"]
    assert evidence
    assert _facts_returned(payload["markdown_response"])["evidence_count"] == len(evidence)
    assert {fact["source_grade"] for fact in evidence} == {"AUTHORITATIVE"}
    assert {fact["entity"] for fact in evidence} == {"NCT05151731"}
    assert "선정·제외 기준" in {fact["metric"] for fact in evidence}
    assert all("확인할 수 없습니다" not in fact["value"] for fact in evidence)


def test_unbranded_clinical_review_uses_disease_query_not_full_question_as_drug() -> None:
    question = "고지혈증 질환(성분)의 임상·허가심사 단계 경쟁약물 현황을 알려줘 ."

    choices = _deterministic_tool_choices(question, BrandResolver())

    assert [(choice.name, choice.arguments) for choice in choices] == [
        ("clinicaltrials_v2_search", {"query": "hyperlipidemia", "query_type": "condition"}),
        ("mfds_clinical_trial_kr", {"query": "고지혈증", "query_type": "condition"}),
    ]


def test_clinicaltrials_prefix_dme_forces_list_only_condition_lookup() -> None:
    question = "ClinicalTrials: 당뇨황반부종(DME) 질환의 임상·허가심사 단계 경쟁약물 현황"

    choices = _deterministic_tool_choices(question, BrandResolver())

    assert [(choice.name, choice.arguments) for choice in choices] == [
        ("clinicaltrials_v2_search", {"query": "diabetic macular edema", "query_type": "condition"}),
    ]


def test_legacy_t2_dme_reuses_shared_disease_translation() -> None:
    question = "당뇨황반부종(DME) 질환(성분)의 임상·허가심사"

    choices = _deterministic_tool_choices(question, BrandResolver())

    assert [(choice.name, choice.arguments) for choice in choices] == [
        ("clinicaltrials_v2_search", {"query": "diabetic macular edema", "query_type": "condition"}),
        ("mfds_clinical_trial_kr", {"query": "당뇨황반부종", "query_type": "condition"}),
    ]


def test_force_contract_flag_prevents_empty_tool_calls_for_exact_live_question(monkeypatch) -> None:
    question = "고지혈증 질환(성분)의 임상·허가심사 단계 경쟁약물 현황을 알려줘 ."
    provider = _ChoiceSequence((ToolChoice(None, {}, "done", call_id=None),))
    monkeypatch.setenv("CHAT_EXTERNAL_TOOL_FORCE_CONTRACT_CALLS", "true")
    monkeypatch.setattr(
        integration_module.GenosToolChoiceProvider,
        "from_env",
        classmethod(lambda cls: provider),
    )

    payload = run_external_tool_agent(
        question,
        resolver=BrandResolver(),
        external=ExternalApiClient(mode="fixture"),
    )

    assert [call["tool"] for call in payload["tool_calls"]] == [
        "clinicaltrials_v2_search",
        "mfds_clinical_trial_kr",
    ]
    assert payload["tool_calls"]
    assert payload["agent_loop_metrics"]["status"] == "partial"
    assert "식약처 허가정보" in payload["answer"]


def test_exact_stroke_review_runs_contract_tools_concurrently(monkeypatch) -> None:
    question = "뇌경색 임상·허가 경쟁약물"
    provider = _ChoiceSequence((ToolChoice(None, {}, "done", call_id=None),))
    external = ExternalApiClient(mode="fixture")
    barrier = threading.Barrier(2)
    intervals: dict[str, tuple[float, float]] = {}
    events: list[dict[str, object]] = []
    original_clinical = external.clinicaltrials_v2_search
    original_domestic = external.mfds_clinical_trial_kr

    def concurrent_call(name: str, call):
        def wrapped(*args, **kwargs):
            started = time.perf_counter()
            barrier.wait(timeout=0.5)
            time.sleep(0.05)
            result = call(*args, **kwargs)
            intervals[name] = (started, time.perf_counter())
            return result

        return wrapped

    monkeypatch.setenv("CHAT_EXTERNAL_TOOL_FORCE_CONTRACT_CALLS", "true")
    monkeypatch.setattr(
        integration_module.GenosToolChoiceProvider,
        "from_env",
        classmethod(lambda cls: provider),
    )
    monkeypatch.setattr(
        external,
        "clinicaltrials_v2_search",
        concurrent_call("clinicaltrials_v2_search", original_clinical),
    )
    monkeypatch.setattr(
        external,
        "mfds_clinical_trial_kr",
        concurrent_call("mfds_clinical_trial_kr", original_domestic),
    )
    with stage_event_sink(events.append):
        payload = run_external_tool_agent(
            question,
            resolver=BrandResolver(),
            external=external,
        )

    assert [call["tool"] for call in payload["tool_calls"]] == [
        "clinicaltrials_v2_search",
        "mfds_clinical_trial_kr",
    ]
    assert provider.calls == 0
    assert all(
        left[0] < right[1] and right[0] < left[1]
        for index, left in enumerate(intervals.values())
        for right in list(intervals.values())[index + 1 :]
    )
    tool_events = [
        event
        for event in events
        if event.get("name")
        in {"임상 데이터 조회", "국내 임상 정보 확인"}
    ]
    assert [event["status"] for event in tool_events[:2]] == ["started"] * 2
    assert {event["name"] for event in tool_events[:2]} == {
        "임상 데이터 조회",
        "국내 임상 정보 확인",
    }


def test_agent_executor_continues_when_completion_policy_requires_final_tool() -> None:
    # Given: the planner first grounds a molecule, then selects the requested patent tool.
    provider = _ChoiceSequence(
        (
            ToolChoice("grounding_tool", {}, "ground the ingredient", call_id="call-1"),
            ToolChoice("patent_tool", {}, "fetch patent evidence", call_id="call-2"),
        )
    )
    grounding = ToolSpec(
        name="grounding_tool",
        description="when to use: grounding. when NOT to use: final patent evidence.",
        input_model=_NoInput,
        execute=lambda _payload: ToolEnvelope(
            ok=True,
            preview="ingredient grounded",
            evidence=(_fact(),),
            raw={"private": "not for planner"},
            error_code=None,
            error_message=None,
        ),
        timeout_s=1.0,
        tags=("local", "grounding"),
    )
    patent_fact = EvidenceFact(
        fact_id="patent:1",
        subject="리바로",
        metric="국내 특허",
        value=None,
        unit=None,
        period="2018-05-08",
        source_name="식약처 의약품 특허 정보",
        source_locator="10-0830018",
        raw_ref="mfds_patent:1",
    )
    patent = ToolSpec(
        name="patent_tool",
        description="when to use: patent evidence. when NOT to use: ingredients only.",
        input_model=_NoInput,
        execute=lambda _payload: ToolEnvelope(
            ok=True,
            preview="patent verified",
            evidence=(patent_fact,),
            raw={"provider_payload": "private"},
            error_code=None,
            error_message=None,
        ),
        timeout_s=1.0,
        tags=("external", "patent"),
    )

    def completion_policy(*, user_text, ledger, spec, tool_calls):
        del user_text, tool_calls
        return ledger.is_complete() and "grounding" not in spec.tags

    # When: the executor applies a verification policy instead of treating any fact as final.
    result = AgentExecutor(provider=provider, completion_policy=completion_policy).run(
        user_text="리바로 특허 만료일",
        tools=(grounding, patent),
    )

    # Then: both steps run, only evidence crosses the planner boundary, and final evidence is rendered.
    assert result.status == "ok"
    assert provider.calls == 2
    assert [call["tool"] for call in result.tool_calls] == ["grounding_tool", "patent_tool"]
    assert "10-0830018" in result.answer
    assert "provider_payload" not in result.answer


def test_tool_catalog_has_descriptions_for_all_22_tools() -> None:
    # Given: the phase-1 external tool inventory.
    records = TOOL_DESCRIPTION_CATALOG

    # When: descriptions are checked as the routing contract.
    descriptions = tuple(record.description.casefold() for record in records)

    # Then: every tool has explicit positive and negative guidance.
    assert len(records) == 22
    assert len({record.name for record in records}) == 22
    assert all("when to use" in description for description in descriptions)
    assert all("when not" in description for description in descriptions)


def test_tool_descriptions_route_trials_and_web_topics_without_misclassifying_guidelines() -> None:
    descriptions = {record.name: record.description for record in TOOL_DESCRIPTION_CATALOG}

    assert "비한정" in descriptions["clinicaltrials_v2_search"]
    assert "비한정" in descriptions["mfds_clinical_trial_kr"]
    assert "가이드라인" in descriptions["web_search"]
    assert "최신 가이드라인은 topic=general" in descriptions["web_search"]
    assert "뉴스" in descriptions["web_search"]
    assert "topic=news" in descriptions["web_search"]


def test_registry_exposes_a_spec_for_every_cataloged_tool() -> None:
    # Given: the real fixture-backed external client and local resolver.
    registry = ExternalToolRegistry(resolver=BrandResolver(), external=ExternalApiClient(mode="fixture"))

    # When: the external tool pack is built.
    specs = registry.list_for_query("외부 근거 조회")

    # Then: every cataloged tool is executable and names are identical.
    assert len(specs) == 22
    assert {spec.name for spec in specs} == {record.name for record in TOOL_DESCRIPTION_CATALOG}


def test_mcp_specs_allow_the_client_timeout_to_finish() -> None:
    # Given: the MCP client owns its transport timeout and returns a structured failure at that boundary.
    external = ExternalApiClient(mode="fixture", timeout_s=12)
    registry = ExternalToolRegistry(resolver=BrandResolver(), external=external)

    # When: wrapper timeouts are compared with the transport timeout.
    mcp_specs = tuple(
        spec
        for spec in registry.list_for_query("외부 근거 조회")
        if spec.name not in {"local_molecule_lookup", "web_search"}
    )

    # Then: the wrapper cannot preempt a structured MCP response at the same deadline.
    assert mcp_specs
    expected_wrapper_budget = MCP_FIRST_ATTEMPT_TIMEOUT_S + external.timeout_s + 1.0
    assert all(spec.timeout_s == expected_wrapper_budget for spec in mcp_specs)


def test_permission_search_and_detail_share_the_tool_timeout_budget(monkeypatch) -> None:
    class McpResponse:
        def __init__(self, result: list[dict[str, str]]) -> None:
            event = {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "content": [],
                    "structuredContent": {"result": result},
                },
            }
            self.text = f"data: {json.dumps(event)}\n\n"

        def raise_for_status(self) -> None:
            return None

    observed: list[tuple[str, float]] = []
    detail_attempts = 0

    def fake_post(_url, *, json, headers, timeout):
        del headers
        nonlocal detail_attempts
        tool_name = str(json["params"]["name"])
        observed.append((tool_name, float(timeout)))
        if tool_name == "search_drug_permission_list":
            time.sleep(0.05)
            return McpResponse(
                [{"ITEM_SEQ": "200500287", "ITEM_NAME": "리바로정1밀리그램"}]
            )
        detail_attempts += 1
        if detail_attempts == 1:
            time.sleep(0.10)
            raise requests.Timeout("detail first attempt disconnected")
        return McpResponse(
            [{"ITEM_SEQ": "200500287", "ITEM_NAME": "리바로정1밀리그램"}]
        )

    monkeypatch.setenv("NEDRUG_MCP_URL", "http://mcp-nedrug/mcp")
    monkeypatch.setattr(
        "jw_chat_agent_poc.tools.external.mcp_client.requests.post",
        fake_post,
    )
    registry = ExternalToolRegistry(
        resolver=BrandResolver(),
        external=ExternalApiClient(mode="live", timeout_s=12),
    )
    permission_spec = next(
        spec
        for spec in registry.list_for_query("리바로 허가정보")
        if spec.name == "mfds_permission_search"
    )
    bounded_spec = replace(permission_spec, timeout_s=0.40)

    envelope = _execute_with_timeout(
        bounded_spec,
        bounded_spec.input_model.model_validate({"brand": "리바로"}),
    )

    assert envelope.ok is True
    assert [name for name, _timeout in observed][:2] == [
        "search_drug_permission_list",
        "get_drug_permission_detail",
    ]
    assert all(
        timeout < bounded_spec.timeout_s
        for name, timeout in observed
        if name == "get_drug_permission_detail"
    )


def test_hira_registry_searches_korean_disease_label_without_internal_mapping() -> None:
    # Given: the planner supplies the Korean disease label and HIRA search owns KCD resolution.
    class _CapturingHiraClient(ExternalApiClient):
        def __init__(self) -> None:
            super().__init__(mode="fixture")
            self.sick_codes: list[str] = []

        def hira_disease_name_code(self, sick_cd: str) -> ExternalCall:
            self.sick_codes.append(sick_cd)
            return super().hira_disease_name_code(sick_cd)

    external = _CapturingHiraClient()
    registry = ExternalToolRegistry(resolver=BrandResolver(), external=external)
    spec = next(spec for spec in registry.list_for_query("고지혈증 환자수") if spec.name == "hira_disease_name_code")

    # When: the HIRA grounding tool crosses the registry boundary.
    envelope = spec.execute(spec.input_model.model_validate({"sick_cd": "고지혈증"}))

    # Then: the label crosses to HIRA search directly instead of using the internal KCD map,
    # and the generic fixture fails closed instead of inventing a code.
    assert envelope.ok is False
    assert envelope.error_code == "NO_DATA"
    assert external.sick_codes == ["고지혈증"]


def test_web_registry_forwards_planner_selected_news_topic() -> None:
    class _CapturingWebClient(ExternalApiClient):
        def __init__(self) -> None:
            super().__init__(mode="fixture")
            self.topics: list[str] = []

        def web_search(self, query: str, max_results: int = 5, *, topic: str = "general") -> ExternalCall:
            self.topics.append(topic)
            return super().web_search(query, max_results=max_results, topic=topic)

    external = _CapturingWebClient()
    registry = ExternalToolRegistry(resolver=BrandResolver(), external=external)
    spec = next(spec for spec in registry.list_for_query("최신 가이드라인") if spec.name == "web_search")

    envelope = spec.execute(
        spec.input_model.model_validate(
            {"query": "최신 고지혈증 가이드라인", "topic": "news"}
        )
    )

    assert envelope.ok is True
    assert external.topics == ["news"]


def test_mfds_clinical_toolspec_preserves_structured_response_as_evidence() -> None:
    class _StructuredMfdsClinicalClient(ExternalApiClient):
        def __init__(self) -> None:
            super().__init__(mode="fixture")

        def mfds_clinical_trial_kr(self, keyword: str, *, query_type: str = "intervention") -> ExternalCall:
            del query_type
            return ExternalCall(
                tool="mfds_clinical_trial_kr",
                source="nedrug_mcp",
                status="live",
                summary_text="MFDS clinical row",
                render_data={
                    "items": [
                        {
                            "GOODS_NAME": "고지혈증 치료제",
                            "CLINIC_STEP_NAME": "3상",
                            "CLNC_TEST_SN": "2026071501",
                        }
                    ],
                    "request": {"query_type": "condition"},
                },
            )

    registry = ExternalToolRegistry(resolver=BrandResolver(), external=_StructuredMfdsClinicalClient())
    spec = next(spec for spec in registry.list_for_query("고지혈증 국내 임상시험") if spec.name == "mfds_clinical_trial_kr")

    envelope = spec.execute(spec.input_model.model_validate({"query": "고지혈증", "query_type": "condition"}))

    assert envelope.ok is True
    assert [(fact.metric, fact.source_name, fact.source_locator) for fact in envelope.evidence] == [
        ("국내 임상시험", "식약처 의약품 정보", "고지혈증 치료제")
    ]


def test_agent_executor_preserves_successful_mfds_clinical_result() -> None:
    class _StructuredMfdsClinicalClient(ExternalApiClient):
        def __init__(self) -> None:
            super().__init__(mode="fixture")

        def mfds_clinical_trial_kr(self, keyword: str, *, query_type: str = "intervention") -> ExternalCall:
            del query_type
            return ExternalCall(
                tool="mfds_clinical_trial_kr",
                source="nedrug_mcp",
                status="live",
                summary_text=f"{keyword} MFDS clinical row",
                render_data={
                    "items": [
                        {
                            "GOODS_NAME": "고지혈증 치료제",
                            "CLINIC_STEP_NAME": "3상",
                            "CLNC_TEST_SN": "2026071501",
                        }
                    ]
                },
            )

    registry = ExternalToolRegistry(resolver=BrandResolver(), external=_StructuredMfdsClinicalClient())
    spec = next(spec for spec in registry.list_for_query("고지혈증 국내 임상시험") if spec.name == "mfds_clinical_trial_kr")
    provider = _ChoiceSequence(
        (
            ToolChoice(
                "mfds_clinical_trial_kr",
                {"query": "고지혈증", "query_type": "condition"},
                "fetch domestic clinical evidence",
                call_id="call-1",
            ),
        )
    )

    result = AgentExecutor(provider=provider).run(user_text="고지혈증 국내 임상시험", tools=(spec,))

    assert result.status == "ok"
    assert result.fallback_code is None
    assert [(trace.tool, trace.status, trace.fallback_code) for trace in result.traces] == [
        ("mfds_clinical_trial_kr", "ok", None)
    ]
    assert "고지혈증 치료제" in result.answer


def test_registry_forwards_mfds_clinical_condition_and_default_intervention() -> None:
    class _CapturingMfdsClinicalClient(ExternalApiClient):
        def __init__(self) -> None:
            super().__init__(mode="fixture")
            self.calls: list[tuple[str, str]] = []

        def mfds_clinical_trial_kr(self, keyword: str, *, query_type: str = "intervention") -> ExternalCall:
            self.calls.append((keyword, query_type))
            return ExternalCall(
                tool="mfds_clinical_trial_kr",
                source="nedrug_mcp",
                status="live",
                summary_text="MFDS clinical row",
                render_data={"items": [{"GOODS_NAME": keyword}]},
            )

    external = _CapturingMfdsClinicalClient()
    registry = ExternalToolRegistry(resolver=BrandResolver(), external=external)
    spec = next(spec for spec in registry.list_for_query("MFDS 임상시험") if spec.name == "mfds_clinical_trial_kr")

    condition = spec.execute(spec.input_model.model_validate({"query": "고지혈증", "query_type": "condition"}))
    intervention = spec.execute(spec.input_model.model_validate({"query": "리바로"}))

    assert condition.ok is True
    assert intervention.ok is True
    assert external.calls == [("고지혈증", "condition"), ("리바로", "intervention")]


def test_fixture_tool_pack_executes_all_22_specs_with_evidence() -> None:
    # Given: schema-valid fixture inputs for every registered external tool.
    payloads: dict[str, dict[str, str]] = {
        "local_molecule_lookup": {"brand": "리바로"},
        "get_drug_main_ingredient": {"brand": "리바로"},
        "openfda_label_search": {"ingredient": "pitavastatin", "evidence_type": "label"},
        "web_search": {"query": "최신 고지혈증 가이드라인"},
        "mfds_permission_search": {"brand": "리바로"},
        "mfds_permission_detail": {"item_seq": "200500287"},
        "mfds_composition": {"brand": "중외"},
        "mfds_easy_drug": {"brand": "활명수"},
        "mfds_clinical_trial_kr": {"query": "리바로"},
        "clinicaltrials_v2_search": {"query": "pitavastatin"},
        "clinicaltrials_study_details": {"nct_id": "NCT05151731"},
        "mfds_patent": {"ingredient": "pitavastatin"},
        "mfds_fda_orangebook": {"ingredient": "pitavastatin"},
        "hira_disease_name_code": {"sick_cd": "E78"},
        "hira_disease_hospitalization_outpatient_stats": {"sick_cd": "E78"},
        "hira_disease_gender_age_stats": {"sick_cd": "E78"},
        "hira_disease_institution_class_stats": {"sick_cd": "E78"},
        "hira_disease_area_stats": {"sick_cd": "E78"},
        "hira_procedure_gender_ipat_opat_stats": {"st5_cd": "MM302"},
        "hira_procedure_gender_age_stats": {"st5_cd": "MM302"},
        "hira_procedure_institution_class_stats": {"st5_cd": "MM302"},
        "hira_procedure_area_stats": {"st5_cd": "MM302"},
    }
    registry = ExternalToolRegistry(resolver=BrandResolver(), external=ExternalApiClient(mode="fixture"))

    # When: each ToolSpec executes through its declared input schema.
    envelopes = {
        spec.name: spec.execute(spec.input_model.model_validate(payloads[spec.name]))
        for spec in registry.list_for_query("fixture census")
    }

    # Then: the census is non-empty and every tool produces verified evidence.
    assert set(envelopes) == set(payloads)
    assert all(envelope.ok and envelope.evidence for envelope in envelopes.values())


def test_openfda_tool_requires_planner_to_choose_label_or_adverse_evidence(monkeypatch) -> None:
    external = ExternalApiClient(mode="fixture")
    monkeypatch.setattr(
        external,
        "openfda_label_search",
        lambda ingredient, *, evidence_type="label": ExternalCall(
            tool="openfda_label_search",
            source="openfda_mcp",
            status="live",
            summary_text="one FAERS report",
            render_data={
                "payload": {
                    "results": [
                        {
                            "safety_report_id": "26558911",
                            "date": "2026-03-31",
                            "reaction_terms": ["Myalgia"],
                            "title": "FAERS report 26558911",
                            "patient": {
                                "drug": [
                                    {
                                        "medicinalproduct": "LIVALO",
                                        "openfda": {"generic_name": ["PITAVASTATIN CALCIUM"]},
                                    }
                                ]
                            },
                        }
                    ]
                },
                "mcp": {"tool": "search_drug_adverse_events"},
            },
        ),
    )
    registry = ExternalToolRegistry(
        resolver=BrandResolver(),
        external=external,
    )

    spec = next(
        tool
        for tool in registry.list_for_query("pitavastatin 부작용")
        if tool.name == "openfda_label_search"
    )
    parameters = spec.openai_schema()["function"]["parameters"]

    assert parameters["properties"]["evidence_type"]["enum"] == ["label", "adverse_event"]
    assert "evidence_type" in parameters["required"]

    envelope = spec.execute(
        spec.input_model.model_validate(
            {"ingredient": "pitavastatin", "evidence_type": "adverse_event"}
        )
    )
    assert envelope.ok is True
    assert {fact.metric for fact in envelope.evidence} == {"FAERS 자발보고 내 이상반응"}


def test_registry_executor_integration_never_exposes_raw_payload() -> None:
    # Given: the planner selects the local evidence tool and then stops.
    provider = _ChoiceSequence(
        (
            ToolChoice("local_molecule_lookup", {"brand": "리바로"}, "local first", call_id="call-1"),
            ToolChoice(None, {}, "enough evidence", call_id=None),
        )
    )

    # When: the complete integration path runs.
    payload = run_external_tool_agent(
        "리바로 성분 알려줘",
        resolver=BrandResolver(),
        external=ExternalApiClient(mode="fixture"),
        provider=provider,
    )

    # Then: deterministic evidence is returned and raw/provider shell fields stay internal.
    wire = json.dumps(payload, ensure_ascii=False)
    assert payload["router_diagnostics"]["mode"] == "tool_use_agent"
    assert "pitavastatin" in payload["answer"]
    assert '"raw"' not in wire
    assert "resultCode" not in wire
    assert "totalCount" not in wire


def test_integration_requires_patent_evidence_after_molecule_grounding() -> None:
    # Given: the planner grounds the brand molecule before choosing the patent tool.
    provider = _ChoiceSequence(
        (
            ToolChoice("local_molecule_lookup", {"brand": "리바로"}, "ground molecule", call_id="call-1"),
            ToolChoice("mfds_patent", {"ingredient": "pitavastatin"}, "fetch patent", call_id="call-2"),
        )
    )

    # When: the production integration applies its evidence-completion policy.
    payload = run_external_tool_agent(
        "리바로 특허 만료일",
        resolver=BrandResolver(),
        external=ExternalApiClient(mode="fixture"),
        provider=provider,
    )

    # Then: molecule grounding alone cannot terminate a patent request.
    assert payload["router_diagnostics"]["fallback_code"] is None
    assert provider.calls == 2
    assert [call["tool"] for call in payload["tool_calls"]] == ["local_molecule_lookup", "mfds_patent"]
    assert "국내 특허" in payload["answer"]


def test_integration_requires_clinical_evidence_for_short_korean_intent() -> None:
    # Given: the planner grounds the molecule before selecting a clinical-trial tool.
    provider = _ChoiceSequence(
        (
            ToolChoice("local_molecule_lookup", {"brand": "리바로"}, "ground molecule", call_id="call-1"),
            ToolChoice(
                "clinicaltrials_v2_search",
                {"query": "pitavastatin"},
                "fetch clinical trials",
                call_id="call-2",
            ),
        )
    )

    # When: the short Korean clinical intent crosses the production completion policy.
    payload = run_external_tool_agent(
        "리바로 임상 알려줘",
        resolver=BrandResolver(),
        external=ExternalApiClient(mode="fixture"),
        provider=provider,
    )

    # Then: molecule grounding cannot terminate a request for clinical evidence.
    assert payload["router_diagnostics"]["fallback_code"] is None
    assert provider.calls == 2
    assert [call["tool"] for call in payload["tool_calls"]] == [
        "local_molecule_lookup",
        "clinicaltrials_v2_search",
    ]


def test_pure_external_competitor_question_selects_external_tool_without_market_metric() -> None:
    question = "고지혈증 질환(성분)의 임상·허가심사 단계 경쟁약물 현황을 알려줘"
    provider = _ChoiceSequence(
        (
            ToolChoice(
                "clinicaltrials_v2_search",
                {"query": "hyperlipidemia competitors"},
                "fetch competing clinical programs",
                call_id="call-1",
            ),
            ToolChoice(
                "mfds_permission_search",
                {"brand": "리바로"},
                "fetch permission evidence",
                call_id="call-2",
            ),
            ToolChoice(
                "local_molecule_lookup",
                {"brand": "리바로"},
                "fetch ingredient evidence",
                call_id="call-3",
            ),
        )
    )

    payload = run_external_tool_agent(
        question,
        resolver=BrandResolver(),
        external=ExternalApiClient(mode="fixture"),
        provider=provider,
    )

    tools = [call["tool"] for call in payload["tool_calls"]]
    assert payload["router_diagnostics"]["fallback_code"] is None
    assert tools == ["clinicaltrials_v2_search", "mfds_permission_search", "local_molecule_lookup"]
    assert "get_brand_metric" not in tools


def test_combined_clinical_permission_question_requests_both_external_sources() -> None:
    requirements = tool_use_requirements("고지혈증 질환(성분)의 임상·허가심사 단계 경쟁약물 현황을 알려줘")

    assert [requirement.label for requirement in requirements] == ["허가 정보", "글로벌 임상시험", "성분"]
    assert all("get_brand_metric" not in requirement.alternatives for requirement in requirements)


def test_integration_requires_orangebook_evidence_for_korean_expiry_intent() -> None:
    # Given: the planner grounds the molecule before selecting the Orange Book tool.
    provider = _ChoiceSequence(
        (
            ToolChoice("local_molecule_lookup", {"brand": "리바로"}, "ground molecule", call_id="call-1"),
            ToolChoice(
                "mfds_fda_orangebook",
                {"ingredient": "pitavastatin"},
                "fetch Orange Book evidence",
                call_id="call-2",
            ),
        )
    )

    # When: the Korean Orange Book expiry intent crosses the completion policy.
    payload = run_external_tool_agent(
        "리바로 오렌지북 만료일 알려줘",
        resolver=BrandResolver(),
        external=ExternalApiClient(mode="fixture"),
        provider=provider,
    )

    # Then: molecule grounding cannot terminate a request for patent evidence.
    assert payload["router_diagnostics"]["fallback_code"] is None
    assert provider.calls == 2
    assert [call["tool"] for call in payload["tool_calls"]] == [
        "local_molecule_lookup",
        "mfds_fda_orangebook",
    ]


def test_integration_accepts_openfda_evidence_for_safety_question() -> None:
    # Given: the requested safety evidence is supplied by the dedicated label tool.
    provider = _ChoiceSequence(
        (
            ToolChoice(
                "openfda_label_search",
                {"ingredient": "pitavastatin", "evidence_type": "adverse_event"},
                "fetch adverse-event evidence",
                call_id="call-1",
            ),
        )
    )

    # When: the production completion policy evaluates the safety question.
    payload = run_external_tool_agent(
        "pitavastatin 안전성 알려줘",
        resolver=BrandResolver(),
        external=ExternalApiClient(mode="fixture"),
        provider=provider,
    )

    # Then: OpenFDA evidence is final evidence, not an incomplete clinical-trial request.
    assert payload["router_diagnostics"]["fallback_code"] is None
    assert [call["tool"] for call in payload["tool_calls"]] == ["openfda_label_search"]
    assert "FDA 의약품 라벨 정보" in payload["answer"]


def _completed_external_call(tool: str, metric: str) -> dict[str, object]:
    return {
        "tool": tool,
        "status": "live",
        "render_data": {
            "status": "live",
            "ok": True,
            "evidence": [
                {
                    "fact_id": f"{tool}:1",
                    "subject": "pitavastatin",
                    "metric": metric,
                    "value": None,
                    "unit": None,
                    "period": None,
                    "source_name": tool,
                    "source_locator": None,
                    "raw_ref": None,
                }
            ],
        },
    }


def test_adverse_event_completion_rejects_plain_label_evidence() -> None:
    label = _completed_external_call("openfda_label_search", "FDA 라벨")
    adverse = _completed_external_call(
        "openfda_label_search",
        "FAERS 자발보고 내 이상반응",
    )

    assert tool_use_evidence_complete("pitavastatin 부작용", [label]) is False
    assert tool_use_evidence_complete("pitavastatin 부작용", [adverse]) is True


def test_orangebook_completion_rejects_domestic_patent_evidence() -> None:
    domestic = _completed_external_call("mfds_patent", "국내 특허")
    orangebook = _completed_external_call("mfds_fda_orangebook", "미국 특허/독점권")

    assert tool_use_evidence_complete("pitavastatin 오렌지북", [domestic]) is False
    assert tool_use_evidence_complete("pitavastatin 오렌지북", [orangebook]) is True


def test_domestic_clinical_completion_rejects_global_trial_evidence() -> None:
    global_trial = _completed_external_call("clinicaltrials_v2_search", "글로벌 임상시험")
    domestic_trial = _completed_external_call("mfds_clinical_trial_kr", "국내 임상시험")

    assert tool_use_evidence_complete("리바로 국내 임상시험", [global_trial]) is False
    assert tool_use_evidence_complete("리바로 국내 임상시험", [domestic_trial]) is True


def test_integration_requires_all_hira_distribution_tools() -> None:
    # Given: a patient-distribution question needs every distribution dimension.
    provider = _ChoiceSequence(
        (
            ToolChoice("hira_disease_name_code", {"sick_cd": "E78"}, "ground KCD", call_id="call-1"),
            ToolChoice(
                "hira_disease_hospitalization_outpatient_stats",
                {"sick_cd": "E78"},
                "fetch inpatient and outpatient",
                call_id="call-2",
            ),
            ToolChoice(
                "hira_disease_gender_age_stats",
                {"sick_cd": "E78"},
                "fetch gender and age",
                call_id="call-3",
            ),
            ToolChoice(
                "hira_disease_institution_class_stats",
                {"sick_cd": "E78"},
                "fetch institution class",
                call_id="call-4",
            ),
            ToolChoice(
                "hira_disease_area_stats",
                {"sick_cd": "E78"},
                "fetch area",
                call_id="call-5",
            ),
        )
    )

    # When: the tool-use agent answers a full patient-distribution request.
    payload = run_external_tool_agent(
        "고지혈증 환자 분포 알려줘",
        resolver=BrandResolver(),
        external=ExternalApiClient(mode="fixture"),
        provider=provider,
    )

    # Then: it cannot stop after the first successful statistic.
    assert payload["router_diagnostics"]["fallback_code"] is None
    assert [call["tool"] for call in payload["tool_calls"]] == [
        "hira_disease_name_code",
        "hira_disease_hospitalization_outpatient_stats",
        "hira_disease_gender_age_stats",
        "hira_disease_institution_class_stats",
        "hira_disease_area_stats",
    ]


def test_integration_requires_five_hira_years_for_trend() -> None:
    # Given: the established HIRA trend contract spans five distinct years.
    choices = [ToolChoice("hira_disease_name_code", {"sick_cd": "E78"}, "ground KCD", call_id="call-1")]
    choices.extend(
        ToolChoice(
            "hira_disease_hospitalization_outpatient_stats",
            {"sick_cd": "E78", "year": str(year)},
            f"fetch {year}",
            call_id=f"call-{index}",
        )
        for index, year in enumerate(range(2020, 2025), start=2)
    )
    provider = _ChoiceSequence(tuple(choices))

    # When: the tool-use agent answers a patient trend request.
    payload = run_external_tool_agent(
        "고지혈증 환자수 추이",
        resolver=BrandResolver(),
        external=ExternalApiClient(mode="fixture"),
        provider=provider,
    )

    # Then: all five yearly statistics are required before deterministic rendering.
    assert payload["router_diagnostics"]["fallback_code"] is None
    years = [
        fact["period"]
        for call in payload["tool_calls"]
        if call["tool"] == "hira_disease_hospitalization_outpatient_stats"
        for fact in call["render_data"]["evidence"]
        if fact.get("period")
    ]
    assert set(years) == {"2020", "2021", "2022", "2023", "2024"}


def test_genos_provider_parses_strict_tool_call(monkeypatch) -> None:
    # Given: an OpenAI-compatible GenOS tool-call response.
    posted: dict[str, Any] = {}

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-7",
                                    "type": "function",
                                    "function": {
                                        "name": "local_molecule_lookup",
                                        "arguments": '{"brand":"리바로"}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            }

    def fake_post(url: str, **kwargs: Any) -> _Response:
        posted.update({"url": url, **kwargs})
        return _Response()

    monkeypatch.setattr("jw_chat_agent_poc.tool_use.provider.requests.post", fake_post)
    provider = GenosToolChoiceProvider(
        base_url="https://planner.example",
        token="dummy-token",
        model="planner",
        max_tokens=512,
    )

    # When: the provider chooses from one strict function schema.
    choice = provider.choose(
        user_text="리바로 성분",
        messages=[{"role": "user", "content": "리바로 성분"}],
        tools=[{"type": "function", "function": {"name": "local_molecule_lookup"}}],
    )

    # Then: arguments and call identity are preserved and planning is deterministic.
    assert choice == ToolChoice("local_molecule_lookup", {"brand": "리바로"}, "리바로 성분", call_id="call-7")
    assert posted["url"] == "https://planner.example/chat/completions"
    assert posted["json"]["temperature"] == 0
    assert posted["json"]["max_tokens"] == 512
    assert posted["json"]["parallel_tool_calls"] is False
    assert posted["json"]["tool_choice"] == "auto"


def test_genos_provider_omits_tool_fields_for_no_tool_question(monkeypatch) -> None:
    # Given: the v4 planner classified a general-help request with no eligible tools.
    posted: dict[str, Any] = {}

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"choices": [{"message": {"content": "사용 방법을 안내합니다."}}]}

    def fake_post(url: str, **kwargs: Any) -> _Response:
        posted.update({"url": url, **kwargs})
        return _Response()

    monkeypatch.setattr("jw_chat_agent_poc.tool_use.provider.requests.post", fake_post)
    provider = GenosToolChoiceProvider(
        base_url="https://planner.example",
        token="dummy-token",
        model="planner",
    )

    # When: the provider classifies the request without any external tool schema.
    choice = provider.choose(
        user_text="이 챗봇 어떻게 쓰는 거야?",
        messages=[{"role": "user", "content": "이 챗봇 어떻게 쓰는 거야?"}],
        tools=[],
    )

    # Then: the legacy provider payload remains unchanged and the natural no-tool answer survives.
    assert choice == ToolChoice(None, {}, "사용 방법을 안내합니다.", call_id=None)
    assert "max_tokens" not in posted["json"]
    assert "tools" not in posted["json"]
    assert "tool_choice" not in posted["json"]
    assert "parallel_tool_calls" not in posted["json"]


def test_tool_use_agent_answer_uses_guarded_markdown_generation_when_configured(monkeypatch) -> None:
    # Given: completed external evidence and a configured final-answer token.
    calls: list[tuple[str, str]] = []

    def natural_markdown(_self, question: str, markdown_response: dict, *_args, **_kwargs) -> str:
        calls.append((question, str(markdown_response["fact_md"])))
        return "마운자로의 주성분은 TIRZEPATIDE입니다.\n\n## 1. 근거 데이터\n\n- 로컬 시장 DB에서 확인했습니다."

    monkeypatch.setattr(GenosClient, "_markdown_answer", natural_markdown)
    agent_result = {
        "answer": "- 마운자로: 성분 = TIRZEPATIDE [로컬 시장 DB 성분 정보]",
        "router_diagnostics": {"mode": "tool_use_agent", "fallback_code": None},
        "tool_calls": [],
        "markdown_response": {
            "fact_md": "- 마운자로: 성분 = TIRZEPATIDE [로컬 시장 DB 성분 정보]",
            "data_md": "",
        },
    }

    # When: the service streams the verified answer.
    answer = "".join(GenosClient(token="dummy-token").stream_answer("마운자로 성분", agent_result))

    # Then: verified facts enter the normal natural-language synthesis path.
    assert answer.startswith("마운자로의 주성분은 TIRZEPATIDE입니다.")
    assert "## 1. 근거 데이터" in answer
    assert calls == [("마운자로 성분", agent_result["markdown_response"]["fact_md"])]


def test_clinical_detail_answer_relays_verified_disclosure_without_final_llm(monkeypatch) -> None:
    def fail_markdown(*_args, **_kwargs) -> str:
        raise AssertionError("verified ClinicalTrials detail must not be rewritten by the final LLM")

    monkeypatch.setattr(GenosClient, "_markdown_answer", fail_markdown)
    url = "https://clinicaltrials.gov/study/NCT05151731"
    fact_md = (
        "- NCT05151731: 연구 제목 = A Study to Investigate Vamikibart "
        f"[{url}]\n"
        "- NCT05151731: 선정·제외 기준 = Adults with diabetic macular edema "
        "(선정·제외기준은 현재 연결에서 앞부분 200자까지만 제공됩니다.) "
        f"[{url}]"
    )
    agent_result = {
        "answer": fact_md,
        "router_diagnostics": {"mode": "tool_use_agent", "fallback_code": None},
        "tool_calls": [{"tool": "clinicaltrials_study_details", "status": "ok"}],
        "markdown_response": {"fact_md": fact_md, "data_md": ""},
    }

    answer = "".join(
        GenosClient(token="dummy-token").stream_answer(
            "NCT05151731 임상 디자인(대상, 평가변수, 기간)을 알려줘",
            agent_result,
        )
    )

    assert "A Study to Investigate Vamikibart" in answer
    assert "선정·제외기준은 현재 연결에서 앞부분 200자까지만 제공됩니다." in answer
    assert f"전문은 ClinicalTrials.gov에서 확인하십시오: {url}" in answer


def test_mfds_composition_answer_relays_verified_facts_without_final_llm(monkeypatch) -> None:
    def fail_markdown(*_args, **_kwargs) -> str:
        raise AssertionError("verified MFDS composition must not be rewritten by the final LLM")

    monkeypatch.setattr(GenosClient, "_markdown_answer", fail_markdown)
    fact_md = (
        "- 리바로: 성분 조성 = 리바로정1밀리그램 · "
        "피타바스타틴칼슘수화물 1.0밀리그램 [식약처 의약품 성분 정보]\n"
        "- 리바로: 성분 조성 = 리바로정2밀리그램 · "
        "피타바스타틴칼슘수화물 2.00밀리그램 [식약처 의약품 성분 정보]"
    )
    agent_result = {
        "answer": fact_md,
        "router_diagnostics": {"mode": "tool_use_agent", "fallback_code": None},
        "tool_calls": [{"tool": "mfds_composition", "status": "ok"}],
        "markdown_response": {"fact_md": fact_md, "data_md": ""},
    }

    answer = "".join(
        GenosClient(token="dummy-token").stream_answer(
            "NeDrug: 리바로 성분 조성 알려줘",
            agent_result,
        )
    )

    assert answer == fact_md


def test_tool_use_agent_answer_without_final_token_remains_natural_and_deterministic() -> None:
    agent_result = {
        "answer": "- 리바로: 성분 = pitavastatin [로컬 시장 DB 성분 정보]",
        "router_diagnostics": {"mode": "tool_use_agent", "fallback_code": None},
        "tool_calls": [],
        "markdown_response": {
            "fact_md": "- 리바로: 성분 = pitavastatin [로컬 시장 DB 성분 정보]",
            "data_md": "",
        },
    }

    answer = "".join(GenosClient(token=None).stream_answer("리바로 성분", agent_result))

    assert answer == (
        "리바로의 주성분은 pitavastatin입니다.\n\n"
        "## 근거 데이터\n\n"
        "- 리바로: 성분 = pitavastatin [로컬 시장 DB 성분 정보]"
    )


def test_disease_identity_answer_is_natural_without_reclassifying_as_ingredient() -> None:
    agent_result = {
        "answer": "- E78: 질병명/상병코드 = 지질단백질대사장애 및 기타 지질증 [건강보험심사평가원 통계]",
        "router_diagnostics": {"mode": "tool_use_agent", "fallback_code": None},
        "tool_calls": [],
        "markdown_response": {
            "fact_md": "- E78: 질병명/상병코드 = 지질단백질대사장애 및 기타 지질증 [건강보험심사평가원 통계]",
            "data_md": "",
        },
    }

    answer = "".join(GenosClient(token=None).stream_answer("리바로 질환", agent_result))

    assert answer == (
        "리바로는 건강보험심사평가원 통계 기준 상병코드 E78, "
        "질병명 '지질단백질대사장애 및 기타 지질증'에 해당합니다.\n\n"
        "## 근거 데이터\n\n"
        "- E78: 질병명/상병코드 = 지질단백질대사장애 및 기타 지질증 [건강보험심사평가원 통계]"
    )
    assert "성분" not in answer


def test_disease_identity_answer_replaces_contaminated_synthesis_but_keeps_sources() -> None:
    fact_md = "- E78: 질병명/상병코드 = 지질단백질대사장애 및 기타 지질증 [건강보험심사평가원 통계]"
    contaminated = (
        "리바로는 피타바스타틴 성분의 치료제입니다. 웹 검색에서 안전성도 확인했습니다.\n\n"
        "## 출처\n\n"
        "| 출처 | 기준기간 |\n"
        "| --- | --- |\n"
        "| HIRA | 해당 없음 |"
    )

    revised = ensure_natural_fact_lead("리바로 질환", contaminated, fact_md)

    assert revised == (
        "리바로는 건강보험심사평가원 통계 기준 상병코드 E78, "
        "질병명 '지질단백질대사장애 및 기타 지질증'에 해당합니다.\n\n"
        "## 근거 데이터\n\n"
        "- E78: 질병명/상병코드 = 지질단백질대사장애 및 기타 지질증 [건강보험심사평가원 통계]\n\n"
        "## 출처\n\n"
        "| 출처 | 기준기간 |\n"
        "| --- | --- |\n"
        "| HIRA | 해당 없음 |"
    )
    assert "피타바스타틴" not in revised
    assert "웹 검색" not in revised


def test_numeric_evidence_preserves_decimal_without_inventing_zero() -> None:
    # Given: one numeric fact and one explicit missing value.
    facts = (
        _fact().model_copy(update={"metric": "점유율", "value": Decimal("29.52"), "unit": "%", "source_locator": None}),
        _fact().model_copy(update={"metric": "결손", "source_locator": None}),
    )

    # When: evidence is rendered.
    answer = render_evidence_answer(facts)

    # Then: the verified decimal is preserved and missing is never coerced to zero.
    assert "29.52%" in answer
    assert "결손 = 0" not in answer


def test_internal_gateway_url_is_not_promoted_to_public_evidence() -> None:
    # Given: a live-style row has a numeric value but no public locator fields.
    call = ExternalCall(
        tool="hira_disease_gender_age_stats",
        source="hira_disease",
        status="success",
        summary_text="HIRA result",
        render_data={"items": [{"ptntCnt": "12"}]},
        safe_url="http://llmops-gateway-api-service:8080/mcp/253/mcp",
    )

    # When: the result is normalized to public evidence.
    envelope = _external_call_envelope(call, "E78", "질병 성별/연령 통계")
    answer = render_evidence_answer(envelope.evidence)

    # Then: the internal cluster URL remains private.
    assert envelope.evidence[0].source_locator is None
    assert "llmops-gateway" not in answer


def test_fallback_log_omits_raw_question(caplog) -> None:
    # Given: the planner cannot select a matching tool.
    question = "민감한 내부 전략 질문"
    provider = _ChoiceSequence((ToolChoice(None, {}, "no matching tool", call_id=None),))

    # When: the integration records its explicit fallback classification.
    with caplog.at_level("INFO"):
        payload = run_external_tool_agent(
            question,
            resolver=BrandResolver(),
            external=ExternalApiClient(mode="fixture"),
            provider=provider,
        )

    # Then: classification is observable without logging prompt contents.
    assert payload["router_diagnostics"]["fallback_code"] == "UNSUPPORTED_QUERY"
    assert "UNSUPPORTED_QUERY" in caplog.text
    assert question not in caplog.text
