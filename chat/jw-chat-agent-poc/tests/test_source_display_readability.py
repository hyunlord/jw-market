from __future__ import annotations

from copy import deepcopy

from jw_chat_agent_poc.common.source_display import TOOL_SOURCE_LABELS, TOOL_STEP_LABELS
from jw_chat_agent_poc.common.timing import _emit_stage_event, _public_stage_name
from jw_chat_agent_poc.orchestrator.answer_facts import answer_fact_markdown
from jw_chat_agent_poc.orchestrator.markdown_formatting import source_label
from jw_chat_agent_poc.orchestrator.provenance_labels import (
    provenance_source_block,
    provenance_source_block_from_facts,
)
from jw_chat_agent_poc.service.answer_safety import append_deterministic_source_block
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


def test_fact_based_provenance_maps_legacy_external_source_labels() -> None:
    fact_md = """- D69.3: 환자수 = 3,620명 [건강보험심사평가원 통계]
- 아일리아: 허가 품목 = 아일리아주사 [식약처 의약품 정보]
- NCT05151731: 임상 디자인 = DME Study [ClinicalTrials.gov 임상시험 정보]
"""

    block = provenance_source_block_from_facts(fact_md)

    assert "심사평가원(HIRA) 질병통계" in block
    assert "식약처 의약품안전나라(NeDrug)" in block
    assert "ClinicalTrials.gov" in block
    assert "HIRA 질병정보서비스" not in block
    assert "식약처 의약품 정보" not in block


def test_exact_d693_final_fact_render_matches_call_based_source_label() -> None:
    question = "상병코드 D693의 최근 5개년 환자수 추이를 분석해줘"
    calls = [
        {
            "tool": "get_disease_stats",
            "source": "hira_disease",
            "render_data": {
                "facade_tool": "get_disease_stats",
                "calls": [
                    {
                        "tool": "hira_disease_hospitalization_outpatient_stats",
                        "source": "hira_disease",
                        "render_data": {
                            "request": {"sickCd": "D693", "year": "2024"},
                            "mapping_sickCd": "D69.3",
                            "mapping_disease_name": "특발성 혈소판감소성 자반",
                            "items": [
                                {
                                    "inpatOpat": "전체",
                                    "sickCd": "D69.3",
                                    "sickNm": "특발성 혈소판감소성 자반",
                                    "ptntCnt": 3620,
                                    "year": "2024",
                                }
                            ],
                        },
                    }
                ],
            },
        }
    ]
    fact_md = answer_fact_markdown(calls, ["hira_disease"])

    path_a = provenance_source_block(calls, ["hira_disease"])
    final_answer = append_deterministic_source_block(
        f"{question}\n\n2024년 환자수는 3,620명입니다.",
        fact_md,
    )

    assert "심사평가원(HIRA) 질병통계" in path_a
    assert "심사평가원(HIRA) 질병통계" in final_answer
    assert "HIRA 질병정보서비스" not in final_answer
