from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
import json
from typing import Any

from pydantic import BaseModel

from jw_chat_agent_poc.service.genos_client import GenosClient
from jw_chat_agent_poc.tool_use.catalog import TOOL_DESCRIPTION_CATALOG
from jw_chat_agent_poc.tool_use.contracts import EvidenceFact, ToolEnvelope
from jw_chat_agent_poc.tool_use.executor import AgentExecutor
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


def test_exact_clinical_permission_competitor_question_forces_every_contract_tool() -> None:
    question = "고지혈증 질환(성분)의 임상·허가심사 단계 경쟁약물 현황을 알려줘 ."

    choices = _deterministic_tool_choices(question, BrandResolver())

    assert [choice.name for choice in choices] == [
        "clinicaltrials_v2_search",
        "mfds_clinical_trial_kr",
        "mfds_permission_search",
        "openfda_label_search",
        "local_molecule_lookup",
    ]
    assert all(choice.call_id and choice.call_id.startswith("contract-") for choice in choices)
    assert [choice.name for choice in _deterministic_tool_choices("리바로 임상실험", BrandResolver())] == [
        "clinicaltrials_v2_search"
    ]
    assert [choice.name for choice in _deterministic_tool_choices("마운자로 성분", BrandResolver())] == [
        "local_molecule_lookup"
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
        "mfds_permission_search",
        "openfda_label_search",
        "local_molecule_lookup",
    ]
    assert payload["tool_calls"]


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


def test_tool_catalog_has_descriptions_for_all_19_tools() -> None:
    # Given: the phase-1 external tool inventory.
    records = TOOL_DESCRIPTION_CATALOG

    # When: descriptions are checked as the routing contract.
    descriptions = tuple(record.description.casefold() for record in records)

    # Then: every tool has explicit positive and negative guidance.
    assert len(records) == 19
    assert len({record.name for record in records}) == 19
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
    assert len(specs) == 19
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


def test_hira_registry_reuses_authoritative_disease_code_for_korean_label() -> None:
    # Given: the planner supplies the Korean disease label while the live HIRA API expects KCD.
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

    # Then: the existing authoritative mapping supplies E78 before the live adapter call.
    assert envelope.ok is True
    assert external.sick_codes == ["E78"]


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


def test_fixture_tool_pack_executes_all_19_specs_with_evidence() -> None:
    # Given: schema-valid fixture inputs for every registered external tool.
    payloads: dict[str, dict[str, str]] = {
        "local_molecule_lookup": {"brand": "리바로"},
        "get_drug_main_ingredient": {"brand": "리바로"},
        "openfda_label_search": {"ingredient": "pitavastatin", "evidence_type": "label"},
        "web_search": {"query": "최신 고지혈증 가이드라인"},
        "mfds_permission_search": {"brand": "리바로"},
        "mfds_permission_detail": {"item_seq": "200500287"},
        "mfds_clinical_trial_kr": {"query": "리바로"},
        "clinicaltrials_v2_search": {"query": "pitavastatin"},
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
    assert posted["json"]["parallel_tool_calls"] is False
    assert posted["json"]["tool_choice"] == "auto"


def test_tool_use_agent_answer_bypasses_genos_markdown_generation(monkeypatch) -> None:
    # Given: a completed tool-use result and a configured final-answer token.
    def unexpected_markdown(*_args, **_kwargs) -> str:
        raise AssertionError("completed tool evidence must bypass final LLM generation")

    monkeypatch.setattr(GenosClient, "_markdown_answer", unexpected_markdown)
    agent_result = {
        "answer": "- 리바로: 성분 = pitavastatin [로컬 시장 DB 성분 정보]",
        "router_diagnostics": {"mode": "tool_use_agent", "fallback_code": None},
        "tool_calls": [],
        "markdown_response": {
            "fact_md": "- 리바로: 성분 = pitavastatin [로컬 시장 DB 성분 정보]",
            "data_md": "",
        },
    }

    # When: the service streams the completed answer.
    answer = "".join(GenosClient(token="dummy-token").stream_answer("리바로 성분", agent_result))

    # Then: the deterministic answer is relayed unchanged.
    assert answer == agent_result["answer"]


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
