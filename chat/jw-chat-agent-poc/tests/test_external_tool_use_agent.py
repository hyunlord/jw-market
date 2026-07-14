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
from jw_chat_agent_poc.tool_use.integration import run_external_tool_agent
from jw_chat_agent_poc.tool_use.provider import GenosToolChoiceProvider, ToolChoice
from jw_chat_agent_poc.tool_use.registry import ExternalToolRegistry
from jw_chat_agent_poc.tool_use.registry import _external_call_envelope
from jw_chat_agent_poc.tool_use.renderer import render_evidence_answer
from jw_chat_agent_poc.tool_use.specs import ToolSpec
from jw_chat_agent_poc.resolver import BrandResolver
from jw_chat_agent_poc.tools.external import ExternalApiClient
from jw_chat_agent_poc.tools.external import ExternalCall


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


def test_agent_executor_stops_before_final_llm_when_evidence_is_complete() -> None:
    # Given: one tool call yields evidence and the planner then stops.
    provider = _ChoiceSequence(
        (
            ToolChoice("evidence_tool", {}, "call evidence tool", call_id="call-1"),
            ToolChoice(None, {}, "enough evidence", call_id=None),
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
    assert provider.calls == 2
    assert "pitavastatin" in result.answer
    assert "resultCode" not in result.answer


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


def test_registry_exposes_a_spec_for_every_cataloged_tool() -> None:
    # Given: the real fixture-backed external client and local resolver.
    registry = ExternalToolRegistry(resolver=BrandResolver(), external=ExternalApiClient(mode="fixture"))

    # When: the external tool pack is built.
    specs = registry.list_for_query("외부 근거 조회")

    # Then: every cataloged tool is executable and names are identical.
    assert len(specs) == 19
    assert {spec.name for spec in specs} == {record.name for record in TOOL_DESCRIPTION_CATALOG}


def test_fixture_tool_pack_executes_all_19_specs_with_evidence() -> None:
    # Given: schema-valid fixture inputs for every registered external tool.
    payloads: dict[str, dict[str, str]] = {
        "local_molecule_lookup": {"brand": "리바로"},
        "get_drug_main_ingredient": {"brand": "리바로"},
        "openfda_label_search": {"ingredient": "pitavastatin"},
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
