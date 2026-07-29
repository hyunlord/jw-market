from __future__ import annotations

from typing import Any

from jw_chat_agent_poc.resolver import BrandResolver
from jw_chat_agent_poc.tool_use.registry import ExternalToolRegistry
from jw_chat_agent_poc.tools.external import ExternalApiClient, ExternalCall
from jw_chat_agent_poc.tools.external.client import (
    _clinicaltrials_detail_payload,
    _clinicaltrials_mcp_payload,
)


class _ClinicalDetailClient(ExternalApiClient):
    def __init__(self, detail: dict[str, Any]) -> None:
        super().__init__(mode="fixture")
        self._detail = detail

    def clinicaltrials_study_details(self, nct_id: str) -> ExternalCall:
        return ExternalCall(
            tool="clinicaltrials_study_details",
            source="clinicaltrials_mcp",
            status="live",
            summary_text=f"ClinicalTrials.gov에서 {nct_id} 상세 원문을 확인했습니다.",
            render_data={"detail": self._detail},
            safe_url=f"https://clinicaltrials.gov/study/{nct_id}",
        )


def _execute_detail(
    detail: dict[str, Any],
    *,
    question: str = "NCT05151731 임상 디자인",
):
    registry = ExternalToolRegistry(
        resolver=BrandResolver(),
        external=_ClinicalDetailClient(detail),
    )
    spec = next(
        item
        for item in registry.list_for_query(question)
        if item.name == "clinicaltrials_study_details"
    )
    return spec.execute(spec.input_model.model_validate({"nct_id": "NCT05151731"}))


def test_nct_detail_projects_observed_dates_and_outcomes() -> None:
    envelope = _execute_detail(
        {
            "nct_id": "NCT05151731",
            "title": "DME Study",
            "start_date": "2022-01-12",
            "primary_completion_date": "2024-08-30",
            "outcomes": ["Change in best corrected visual acuity"],
        }
    )

    facts = {fact.metric: fact for fact in envelope.evidence}
    assert envelope.ok is True
    assert facts["시험 시작일"].source_locator.startswith("2022-01-12")
    assert facts["일차 완료일"].source_locator.startswith("2024-08-30")
    assert "Change in best corrected visual acuity" in facts["결과지표"].source_locator


def test_nct_detail_missing_dates_and_empty_outcomes_return_null_reasons() -> None:
    envelope = _execute_detail(
        {
            "nct_id": "NCT05151731",
            "title": "DME Study",
            "outcomes": [],
        }
    )

    facts = {fact.metric: fact for fact in envelope.evidence}
    for metric in ("시험 시작일", "일차 완료일", "결과지표"):
        fact = facts[metric]
        assert fact.value is None
        assert fact.source_locator
        assert "확인할 수 없습니다" in fact.source_locator
        assert "0" not in fact.source_locator
        assert "[]" not in fact.source_locator


def test_nct_trial_design_marks_requested_missing_fields_as_partial() -> None:
    envelope = _execute_detail(
        {
            "nct_id": "NCT05151731",
            "enrollment": 300,
            "outcomes": ["Visual acuity"],
        },
        question="NCT05151731 시험 디자인 알려줘",
    )

    assert envelope.ok is True
    assert envelope.error_code == "PARTIAL_RESULT"
    assert envelope.missing_requested_facets == (
        "start_date",
        "primary_completion_date",
        "allocation",
        "masking",
        "intervention_model",
    )


def test_nct_detail_parser_preserves_trial_design_fields() -> None:
    detail = _clinicaltrials_detail_payload(
        "\n".join(
            (
                "nctId: NCT05151731",
                "officialTitle: A Study of Vamikibart",
                "allocation: RANDOMIZED",
                "masking: QUADRUPLE",
                "interventionModel: PARALLEL",
            )
        )
    )

    assert detail["title"] == "A Study of Vamikibart"
    assert detail["allocation"] == "RANDOMIZED"
    assert detail["masking"] == "QUADRUPLE"
    assert detail["intervention_model"] == "PARALLEL"


def test_nct_search_parser_preserves_trial_design_fields() -> None:
    payload = _clinicaltrials_mcp_payload(
        "\n".join(
            (
                "- nctId: NCT05151731",
                "officialTitle: A Study of Vamikibart",
                "allocation: RANDOMIZED",
                "masking: QUADRUPLE",
                "interventionModel: PARALLEL",
            )
        )
    )

    study = payload["studies"][0]["protocolSection"]
    assert study["identificationModule"]["officialTitle"] == "A Study of Vamikibart"
    assert study["designModule"]["allocation"] == "RANDOMIZED"
    assert study["designModule"]["masking"] == "QUADRUPLE"
    assert study["designModule"]["interventionModel"] == "PARALLEL"


def test_nct_trial_design_projects_design_fields_only_when_requested() -> None:
    detail = {
        "nct_id": "NCT05151731",
        "title": "DME Study",
        "allocation": "RANDOMIZED",
        "masking": "QUADRUPLE",
        "intervention_model": "PARALLEL",
        "enrollment": 394,
        "outcomes": ["Visual acuity"],
        "start_date": "2021-12-31",
        "primary_completion_date": "2024-11-06",
    }

    design = _execute_detail(detail, question="NCT05151731 시험 디자인 알려줘")
    generic = _execute_detail(detail, question="NCT05151731 상태 알려줘")

    design_metrics = {fact.metric for fact in design.evidence}
    generic_metrics = {fact.metric for fact in generic.evidence}
    assert {"배정 방식", "눈가림", "중재 모형"} <= design_metrics
    assert {"배정 방식", "눈가림", "중재 모형"}.isdisjoint(generic_metrics)
