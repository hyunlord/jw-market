from __future__ import annotations

import json

import pytest

from jw_chat_agent_poc.service.v4.contracts import PlannerOutput, SourceResult, ToolQueries
from jw_chat_agent_poc.service.v4.lossless_contracts import (
    CoverageLedger,
    DeterministicRender,
    RenderNode,
)
from jw_chat_agent_poc.service.v4.lossless_spine import compose_lossless_answer
from jw_chat_agent_poc.service.v4.synthesizer import _synthesis_messages


def _rendered(*nodes: RenderNode, notices: tuple[str, ...] = (), bindings: tuple[dict[str, object], ...] = ()) -> DeterministicRender:
    record_ids = tuple(dict.fromkeys(record_id for node in nodes for record_id in node.record_ids))
    return DeterministicRender(
        profile="market_analysis",
        nodes=nodes,
        coverage=CoverageLedger(
            records_received=len(record_ids),
            records_unique=len(record_ids),
            records_rendered=len(record_ids),
        ),
        source_notices=notices,
        source_notice_bindings=bindings,
        narrated_record_ids=record_ids,
    )


def _node(block_id: str, heading: str, rows: tuple[tuple[str, str], ...]) -> RenderNode:
    table = "\n".join(
        (
            "| 항목 | 값 |",
            "| --- | --- |",
            *(f"| {label} | {value} |" for label, value in rows),
        )
    )
    return RenderNode(
        block_id=block_id,
        record_ids=tuple(f"{block_id}:{index}" for index in range(1, len(rows) + 1)),
        text=f"## {heading}\n{table}",
    )


def _coverage(block_id: str, source: str, count: int) -> RenderNode:
    return RenderNode(
        block_id=f"{block_id}:coverage",
        text=(
            "## 조사 범위와 완전성\n"
            f"원천 검색 {count}건 · 수신 {count}건 · 중복 제거 후 {count}건 · 상세 표시 {count}건"
        ),
        surface_fields=(source,),
    )


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
        linking_plan="single wave",
    )


def test_sales_axis_puts_answer_and_market_facts_before_auxiliary_lanes() -> None:
    rendered = _rendered(
        _coverage("clinical", "clinicaltrials", 2),
        _node("clinical:records", "임상시험 상세", (("NCT1", "3상"), ("NCT2", "2상"))),
        _coverage("market", "mart", 2),
        _node("market:records", "시장 데이터", (("2026-01", "83.0억원"), ("2026-02", "84.2억원"))),
    )

    composed = compose_lossless_answer(
        rendered,
        "## 핵심 답\n리바로 월별 매출은 83.0억원에서 84.2억원으로 늘었습니다.",
        synthesis_trace={"status": "synthesized"},
        mode="inject",
        question="리바로 2026년 월별 매출",
    )

    assert composed.text.startswith("## 핵심 답\n리바로 월별 매출")
    assert composed.text.index("## 시장 데이터") < composed.text.index("## 참고 자료")
    assert composed.trace["answer_axis"] == "sales"
    assert composed.trace["primary_source"] == "mart"


def test_unknown_axis_preserves_existing_fact_order_and_records_trace() -> None:
    rendered = _rendered(
        _node("clinical:records", "임상시험 상세", (("NCT1", "3상"),)),
        _node("market:records", "시장 데이터", (("2026-01", "83.0억원"),)),
    )

    composed = compose_lossless_answer(
        rendered,
        "## 핵심 답\n확인 결과입니다.",
        synthesis_trace={"status": "synthesized"},
        mode="inject",
        question="리바로 자료를 설명해줘",
    )

    assert composed.text.index("## 임상시험 상세") < composed.text.index("## 시장 데이터")
    assert composed.trace["answer_axis"] == "unknown"
    assert composed.trace["axis_fallback_preserved_order"] is True


def test_coverage_sections_are_consolidated_into_one_lane_table() -> None:
    rendered = _rendered(
        _coverage("market", "mart", 2),
        _node("market:records", "시장 데이터", (("2026-01", "83.0억원"), ("2026-02", "84.2억원"))),
        _coverage("web", "web", 1),
        _node("web:records", "공개 자료", (("기사", "2026-08-17"),)),
    )

    composed = compose_lossless_answer(
        rendered,
        "## 핵심 답\n매출을 확인했습니다.",
        synthesis_trace={"status": "synthesized"},
        mode="inject",
        question="리바로 매출",
    )

    assert composed.text.count("## 조사 범위와 완전성") == 1
    coverage = composed.text.split("## 조사 범위와 완전성", 1)[1]
    assert "내부 데이터마트" in coverage
    assert "공개 웹 자료" in coverage


def test_missing_primary_axis_is_reported_before_auxiliary_context() -> None:
    rendered = _rendered(
        _node("clinical:records", "임상시험 상세", (("NCT1", "3상"),)),
        notices=("건강보험심사평가원 조회는 완료됐으나 조건에 맞는 자료가 0건입니다.",),
        bindings=(
            {
                "record_id": None,
                "notice": "건강보험심사평가원 조회는 완료됐으나 조건에 맞는 자료가 0건입니다.",
                "reason_code": "empty_result",
                "exposure_layer": "F-scope",
                "tool": "hira",
            },
        ),
    )

    composed = compose_lossless_answer(
        rendered,
        "## 근거와 맥락\n참고: 인접 연구 및 기술 동향입니다.",
        synthesis_trace={"status": "synthesized"},
        mode="inject",
        question="당뇨병 환자수 알려줘",
    )

    assert composed.text.startswith("## 핵심 답\n요청하신 환자수")
    assert "성공했으나 0건" in composed.text.split("\n\n", 1)[0]
    assert composed.text.index("환자수") < composed.text.index("인접 연구")
    assert composed.trace["primary_axis_absence"] == "empty_result"


@pytest.mark.parametrize(
    ("reason_code", "public_reason"),
    (
        ("not_executed", "실행 안 함"),
        ("upstream_timeout", "응답 시간 초과"),
        ("empty_result", "성공했으나 0건"),
        ("quota_exhausted", "쿼터·한도 소진"),
    ),
)
def test_primary_absence_uses_only_the_typed_reason_code(
    reason_code: str,
    public_reason: str,
) -> None:
    rendered = _rendered(
        _node("clinical:records", "임상시험 상세", (("NCT1", "3상"),)),
        notices=("건강보험심사평가원 자료가 없습니다.",),
        bindings=(
            {
                "record_id": None,
                "notice": "건강보험심사평가원 자료가 없습니다.",
                "reason_code": reason_code,
                "exposure_layer": "F-scope",
                "tool": "hira",
            },
        ),
    )

    composed = compose_lossless_answer(
        rendered,
        "## 근거와 맥락\n참고 자료입니다.",
        synthesis_trace={"status": "synthesized"},
        mode="inject",
        question="당뇨병 환자수 알려줘",
    )

    assert composed.text.startswith("## 핵심 답\n요청하신 환자수")
    assert public_reason in composed.text.split("\n\n", 1)[0]
    assert composed.trace["primary_axis_absence"] == reason_code


def test_secondary_lane_is_compact_but_all_record_ids_remain_accounted_for() -> None:
    rendered = _rendered(
        _node("market:records", "시장 데이터", (("2026-01", "83.0억원"),)),
        _node(
            "patent:kr-primary",
            "국내 특허",
            tuple((f"권리자 {index}", f"특허 {index}") for index in range(1, 11)),
        ),
    )

    composed = compose_lossless_answer(
        rendered,
        "## 핵심 답\n리바로 매출을 확인했습니다.",
        synthesis_trace={"status": "synthesized"},
        mode="inject",
        question="리바로 2026년 월별 매출",
    )

    assert "권리자 10" not in composed.text
    assert "국내 특허 10건" in composed.text
    assert "상세 항목은 조회 상세" in composed.text
    assert composed.trace["records_rendered"] == 11
    assert composed.trace["secondary_records_compacted"] == 10


def test_secondary_lane_deduplicates_record_ids_shared_by_summary_and_detail_nodes() -> None:
    shared_ids = ("ct:NCT1", "ct:NCT2")
    rendered = DeterministicRender(
        profile="market_analysis",
        nodes=(
            _node("market:records", "시장 데이터", (("2026-01", "83.0억원"),)),
            RenderNode(
                block_id="clinical:records",
                record_ids=shared_ids,
                text="## 임상시험 집계\n2건을 확인했습니다.",
            ),
            RenderNode(
                block_id="clinical:record-details",
                record_ids=shared_ids,
                text="## 임상시험 상세\n| 시험 | 단계 |\n| --- | --- |\n| NCT1 | 3상 |\n| NCT2 | 2상 |",
            ),
        ),
        coverage=CoverageLedger(records_received=3, records_unique=3, records_rendered=3),
        narrated_record_ids=("market:records:1", *shared_ids),
    )

    composed = compose_lossless_answer(
        rendered,
        "## 핵심 답\n리바로 매출을 확인했습니다.",
        synthesis_trace={"status": "synthesized"},
        mode="inject",
        question="리바로 2026년 월별 매출",
    )

    assert "임상시험 집계 2건" in composed.text
    assert "임상시험 상세" not in composed.text
    assert composed.trace["secondary_records_compacted"] == 2


def test_duplicate_core_headings_merge_into_one() -> None:
    rendered = _rendered(_node("market:records", "시장 데이터", (("2026-01", "83.0억원"),)))

    composed = compose_lossless_answer(
        rendered,
        "## 핵심 답\n첫 답입니다.\n\n## 핵심 요약\n둘째 답입니다.",
        synthesis_trace={"status": "synthesized"},
        mode="inject",
        question="리바로 매출",
    )

    assert composed.text.count("## 핵심 답") == 1
    assert "첫 답입니다." in composed.text
    assert "둘째 답입니다." in composed.text


def test_primary_table_shows_at_most_fifteen_rows_without_losing_record_accounting() -> None:
    rows = tuple((f"2026-{index:02d}", f"{80 + index}.0억원") for index in range(1, 21))
    rendered = _rendered(_node("market:records", "시장 데이터", rows))

    composed = compose_lossless_answer(
        rendered,
        "## 핵심 답\n월별 매출을 확인했습니다.",
        synthesis_trace={"status": "synthesized"},
        mode="inject",
        question="리바로 월별 매출",
    )

    assert "전체 20건 중 15건 표시" in composed.text
    assert "2026-16" not in composed.text
    assert composed.trace["records_rendered"] == 20
    assert composed.trace["primary_table_rows_hidden"] == 5


def test_comparison_observation_section_is_removed_when_market_table_owns_values() -> None:
    rendered = _rendered(_node("market:records", "시장 데이터", (("리바로", "83.0억원"),)))

    composed = compose_lossless_answer(
        rendered,
        (
            "## 핵심 답\n리바로 매출을 확인했습니다.\n\n"
            "## 비교 관측\n리바로와 경쟁 브랜드의 같은 수치를 다시 적었습니다.\n\n"
            "## 종합 인사이트\n시장 데이터 구획을 근거로 복합제 전환 가능성을 검토할 만합니다."
        ),
        synthesis_trace={"status": "synthesized"},
        mode="inject",
        question="리바로 월별 매출",
    )

    assert "## 비교 관측" not in composed.text
    assert "## 종합 인사이트" in composed.text
    assert composed.trace["comparison_observation_sections_removed"] == 1


def test_comparison_facts_require_a_separate_grounded_advisory_section() -> None:
    result = SourceResult(
        source="mart",
        query="리바로 월별 매출",
        status="ok",
        payload={
            "calls": [
                {
                    "render_data": {
                        "market_size_series": [
                            {"period": "2025-09", "value_억원": 100.0},
                            {"period": "2026-06", "value_억원": 130.0},
                        ]
                    }
                },
                {
                    "entity_bundle": {
                        "anchor": "리바로",
                        "period_start": "2025-09",
                        "period_end": "2026-06",
                        "members": [
                            {
                                "brand": "리바로",
                                "role": "target",
                                "render_data": {
                                    "brand_value_series_10pt": [
                                        {"period": "2025-09", "value_억원": 10.0},
                                        {"period": "2026-06", "value_억원": 12.0},
                                    ]
                                },
                            }
                        ],
                    }
                },
            ]
        },
    )

    prompt = json.loads(
        _synthesis_messages(_plan("리바로 월별 매출"), (result,), ())[-1]["content"]
    )

    contract = prompt["advisory_contract"]
    assert contract["section"] == "종합 인사이트"
    assert contract["required_when_facts_exist"] is True
    assert contract["separate_from_fact_surface"] is True
    assert contract["cite_fact_section"] is True
    assert contract["new_numbers_forbidden"] is True
    assert contract["assertive_recommendations_forbidden"] is True
