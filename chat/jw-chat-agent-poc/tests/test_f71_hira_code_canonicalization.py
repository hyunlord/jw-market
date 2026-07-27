from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from jw_chat_agent_poc.orchestrator.provenance import EvidenceFact as BindingEvidenceFact
from jw_chat_agent_poc.service.evidence_binding import verify_claim_bindings
from jw_chat_agent_poc.tool_use.contracts import AgentResult, EvidenceFact
from jw_chat_agent_poc.tool_use.integration import _agent_result_payload
from jw_chat_agent_poc.tool_use.renderer import render_evidence_answer


_CAPTURE_PATH = Path(__file__).parent / "fixtures" / "f71_live" / "08_f48_code.json"
_CAPTURE_SHA256 = "14aad4595be08dc196f8d608da303a40729ba7ca54563160e05cdd9138522421"


def _capture() -> dict[str, Any]:
    raw = _CAPTURE_PATH.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == _CAPTURE_SHA256
    return json.loads(raw)


def _patient_axis(capture: dict[str, Any]) -> dict[str, Any]:
    combinations = capture["qa_trace"]["claims"]["pipeline_observability"]["fact_inventory"][
        "axis_combinations"
    ]
    return next(item for item in combinations if item["metric"] == "환자수")


def _result(raw_fact: EvidenceFact) -> AgentResult:
    return AgentResult(
        status="ok",
        answer=render_evidence_answer((raw_fact,)),
        tool_calls=(
            {
                "tool": "hira_disease_hospitalization_outpatient_stats",
                "source": "external_api",
                "status": "ok",
                "summary_text": "근거 1건 확인",
                "render_data": {
                    "ok": True,
                    "preview": "근거 1건 확인",
                    "evidence": [raw_fact.model_dump(mode="json")],
                    "error_code": None,
                    "error_message": None,
                },
            },
        ),
        sources=(raw_fact.source_name,),
        traces=(),
        fallback_code=None,
    )


def _project_capture(capture: dict[str, Any]) -> tuple[BindingEvidenceFact, ...]:
    patient_axis = _patient_axis(capture)
    raw_fact = EvidenceFact(
        fact_id="hira_disease_hospitalization_outpatient_stats:2",
        subject=patient_axis["entity"],
        metric="질병 입원/외래 통계",
        value=Decimal("34091"),
        unit=None,
        period=patient_axis["period"],
        source_name="건강보험심사평가원 통계",
        source_locator="외래",
        raw_ref="hira_disease_hospitalization_outpatient_stats:2",
    )
    payload = _agent_result_payload(capture["question"], _result(raw_fact))
    return tuple(
        BindingEvidenceFact(**fact) for fact in payload["markdown_response"]["evidence"]
    )


def test_live_f48_capture_projects_original_dotted_code_and_binds() -> None:
    capture = _capture()

    projected = _project_capture(capture)
    verification = verify_claim_bindings(
        question=capture["question"],
        answer="E11.3의 2024년 외래 환자수는 34,091명입니다.",
        facts=projected,
    )

    assert _patient_axis(capture)["entity"] == "E113"
    assert len(capture["qa_trace"]["claims"]["rejections"]) == 13
    assert projected[0].entity == "E11.3"
    assert projected[0].metric == "환자수"
    assert projected[0].unit == "명"
    assert verification.status == "pass"
    assert verification.disposition == "answered"
    assert verification.blocked_reasons == ()


def test_dotless_input_uses_existing_canonical_code() -> None:
    raw_fact = EvidenceFact(
        fact_id="hira_disease_hospitalization_outpatient_stats:1",
        subject="D693",
        metric="질병 입원/외래 통계",
        value=Decimal("3620"),
        unit=None,
        period="2024",
        source_name="건강보험심사평가원 통계",
        source_locator="외래",
        raw_ref="hira_disease_hospitalization_outpatient_stats:1",
    )

    payload = _agent_result_payload(
        "상병코드 D693 최근 5개년 환자수 추이",
        _result(raw_fact),
    )

    assert payload["markdown_response"]["evidence"][0]["entity"] == "D69.3"
