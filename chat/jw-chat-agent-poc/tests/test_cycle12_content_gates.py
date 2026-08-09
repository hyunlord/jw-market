from __future__ import annotations

from jw_chat_agent_poc.orchestrator.answer_claim_adapters import claims_for, render_claim
from jw_chat_agent_poc.orchestrator.external_passthrough_render import (
    finalize_external_passthrough_answer,
)
from jw_chat_agent_poc.service.app import _compute_final_answer
from jw_chat_agent_poc.service.runtime_provenance import _answer_control_layer


def _web_call(*, source_grade: str, content: str, title: str) -> dict[str, object]:
    return {
        "tool": "web_search",
        "status": "success",
        "render_data": {
            "items": [
                {
                    "title": title,
                    "content": content,
                    "source_grade": source_grade,
                    "url": "https://example.test/source",
                }
            ]
        },
    }


def test_v3_cutover_blocks_title_only_prevalence_result() -> None:
    result = {
        "answer": "유튜브 제목: 당뇨망막병증 완전 정복",
        "sources": ["web_search"],
        "tool_calls": [_web_call(source_grade="C 기타·개인", content="", title="당뇨망막병증 완전 정복")],
        "charts": [],
        "v3_cutover_ready": True,
    }

    final = _compute_final_answer("당뇨망막병증 국내 유병률 알려줘", result, "cycle12-q18-red")

    assert final.text == "국내 유병률 근거를 확보하지 못했습니다"


def test_v3_cutover_preserves_supported_prevalence_result() -> None:
    answer = "기관 조사에서 당뇨망막병증 유병률은 12.3%였습니다."
    result = {
        "answer": answer,
        "sources": ["web_search"],
        "tool_calls": [
            _web_call(
                source_grade="B 기관·학술",
                content="2024년 조사에서 당뇨망막병증 유병률은 12.3%였습니다.",
                title="기관 연구",
            )
        ],
        "charts": [],
        "v3_cutover_ready": True,
    }

    final = _compute_final_answer("당뇨망막병증 국내 유병률 알려줘", result, "cycle12-q18-green")

    assert final.text == answer


def test_e1_zero_news_keeps_explicit_news_block_message() -> None:
    claims = claims_for(
        "EXTERNAL_LOOKUP",
        {
            "contract_id": "E1",
            "news_refs": [],
            "internal_brand_metric": {
                "source": "UBIST",
                "period": "2026-06",
                "brand": "리바로",
                "sales_krw": 8_587_000_000,
                "share_pct": 3.72,
                "rank": 6,
                "total_brands": 555,
            },
        },
    )

    rendered = "\n".join(render_claim(claim) for claim in claims)

    assert "조건을 충족한 기사 0건" in rendered


def test_public_trace_projects_cycle12_diagnostics() -> None:
    metadata = {
        "applied": True,
        "block_diagnostics": {
            "news_raw_count": 2,
            "news_after_filter_count": 0,
            "internal_metric_claim_count": 1,
            "rendered_block_count": 2,
        },
        "external_status": "PARTIAL",
        "failed_dimensions": ["hira_disease_hospitalization_outpatient_stats:2022"],
        "failure_reason": "timeout",
        "claim_plan": ["private"],
    }

    projected = _answer_control_layer({"_answer_control_layer": metadata})

    assert projected["block_diagnostics"] == metadata["block_diagnostics"]
    assert projected["external_status"] == "PARTIAL"
    assert projected["failed_dimensions"] == metadata["failed_dimensions"]
    assert projected["failure_reason"] == "timeout"
    assert "claim_plan" not in projected


def test_hira_status_wording_distinguishes_failure_from_normal_empty() -> None:
    result = {
        "tool_calls": [
            {
                "tool": "hira_disease_hospitalization_outpatient_stats",
                "status": "error",
                "render_data": {"requested_period": "2022", "error": "upstream timeout"},
            },
            {
                "tool": "hira_disease_hospitalization_outpatient_stats",
                "status": "no_data",
                "render_data": {"requested_period": "2023"},
            },
        ]
    }
    answer = "- 2022년: 조회 결과가 없습니다.\n- 2023년: 조회 결과가 없습니다."

    rendered = finalize_external_passthrough_answer(answer, result, question="D693 상병 환자수 최근 5년")

    assert (
        "2022년은 HIRA API 응답 실패로 값을 확인하지 못했습니다. "
        "이는 환자수가 0명이라는 의미가 아닙니다."
    ) in rendered
    assert "2023년은 조회가 정상 완료됐으나 해당 결과 행이 없습니다." in rendered
