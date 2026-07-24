from __future__ import annotations

from copy import deepcopy

from jw_chat_agent_poc.common.source_display import TOOL_SOURCE_LABELS, TOOL_STEP_LABELS
from jw_chat_agent_poc.common.timing import _emit_stage_event, _public_stage_name
from jw_chat_agent_poc.orchestrator.markdown_formatting import source_label
from jw_chat_agent_poc.orchestrator.provenance_labels import provenance_source_block
from jw_chat_agent_poc.tool_use.catalog import TOOL_DESCRIPTION_CATALOG


PROVENANCE_HEADER = "| 출처 | 기준기간 | 뷰 | 시장정의 | 분모 | 채널 | 단위 |"


def test_every_registered_external_tool_has_public_step_and_source_labels() -> None:
    registered = {record.name for record in TOOL_DESCRIPTION_CATALOG if record.has_spec}

    assert registered <= TOOL_STEP_LABELS.keys()
    assert registered <= TOOL_SOURCE_LABELS.keys()


def test_external_tool_steps_use_specific_public_names() -> None:
    assert _public_stage_name("answer_generation_total") == "도구 조회 및 답변 작성"
    assert _public_stage_name("tool:mfds_permission_search") == "NeDrug 허가정보 조회 중"
    assert (
        _public_stage_name("tool:hira_disease_hospitalization_outpatient_stats")
        == "HIRA 질병통계 조회 중"
    )
    assert _public_stage_name("tool:clinicaltrials_v2_search") == "ClinicalTrials.gov 조회 중"


def test_parallel_tool_steps_keep_each_public_identity() -> None:
    events: list[dict[str, object]] = []

    _emit_stage_event(events.append, "tool:mfds_permission_search", "step=1; mode=parallel", "started")
    _emit_stage_event(
        events.append,
        "tool:hira_disease_hospitalization_outpatient_stats",
        "step=1; mode=parallel",
        "started",
    )

    assert [event["name"] for event in events] == [
        "NeDrug 허가정보 조회 중",
        "HIRA 질병통계 조회 중",
    ]
    assert all(event["detail"] == "1단계 · 관련 항목 동시 조회" for event in events)


def test_unknown_tool_and_source_never_expose_internal_identifiers() -> None:
    assert _public_stage_name("tool:new_external_provider_lookup") == "외부 데이터 조회 중"
    assert source_label("external") == "외부 데이터 원천"
    assert source_label("new_external_provider") == "외부 데이터 원천"


def test_provenance_uses_tool_specific_source_without_changing_call_payload() -> None:
    calls = [
        {
            "tool": "mfds_permission_search",
            "source": "external",
            "status": "ok",
            "render_data": {"items": [{"item_name": "아일리아"}]},
        },
        {
            "tool": "hira_disease_hospitalization_outpatient_stats",
            "source": "external",
            "status": "ok",
            "render_data": {"items": [{"year": "2025", "patients": 10}]},
        },
    ]
    before = deepcopy(calls)

    block = provenance_source_block(calls, ["external"])

    assert PROVENANCE_HEADER in block
    assert "식약처 의약품안전나라(NeDrug)" in block
    assert "심사평가원(HIRA) 질병통계" in block
    assert "external" not in block
    assert calls == before
