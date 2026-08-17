from __future__ import annotations

from jw_chat_agent_poc.service.v4.lossless_contracts import RenderNode
from jw_chat_agent_poc.service.v4.lossless_spine import compose_lossless_answer
from jw_chat_agent_poc.service.file_sql_query import _resolve_deterministic_select
from jw_chat_agent_poc.service.v4.source_tiers import _completion_entities
from test_chat_v4_r42b import _plan, _rendered


def test_reimbursement_defers_public_market_label_from_core() -> None:
    rendered = _rendered(
        RenderNode(
            block_id="policy:1:info",
            record_ids=("hira:notice:matched",),
            text="## 고시 정보\n| 항목 | 값 |\n| --- | --- |\n| 고시번호 | 제2021-245호 |",
        )
    )
    commentary = (
        "## 핵심 답\n리바로젯 급여기준은 제2021-245호에서 확인했습니다.\n\n"
        "리바로젯 매출은 124.54억원입니다 [출처: 시장 데이터베이스].\n\n"
        "로수젯 점유율은 7.52%입니다 [출처: 시장 데이터베이스]."
    )

    composed = compose_lossless_answer(
        rendered,
        commentary,
        synthesis_trace={"status": "synthesized"},
        mode="inject",
        question="리바로젯 급여기준 알려줘",
    )

    core = composed.text.split("## 핵심 답\n", 1)[1].split("\n## ", 1)[0]
    assert "제2021-245호" in core
    assert "124.54억원" not in core
    assert "7.52%" not in core
    assert "## 참고: 인접 연구" in composed.text
    assert "124.54억원" in composed.text


def test_reimbursement_separates_notice_not_present_in_policy_facts() -> None:
    rendered = _rendered(
        RenderNode(
            block_id="policy:1:info",
            record_ids=("hira:notice:matched",),
            text="## 고시 정보\n| 항목 | 값 |\n| --- | --- |\n| 고시번호 | 제2021-245호 |",
        )
    )
    commentary = (
        "## 핵심 답\n리바로젯 급여기준은 제2021-245호입니다. "
        "고시 제2026-138호에서는 의료급여 일반기준 일부개정이 이루어졌습니다."
    )

    composed = compose_lossless_answer(
        rendered,
        commentary,
        synthesis_trace={"status": "synthesized"},
        mode="inject",
        question="리바로젯 급여기준 알려줘",
    )

    core = composed.text.split("## 핵심 답\n", 1)[1].split("\n## ", 1)[0]
    assert "제2021-245호" in core
    assert "제2026-138호" not in core
    assert "## 참고: 관련 고시" in composed.text
    assert "제2026-138호" in composed.text


def test_patient_table_replaces_homogeneous_model_restatement() -> None:
    rendered = _rendered(
        RenderNode(
            block_id="hira-statistics:records",
            record_ids=("hira:E10:0", "hira:E10:10", "hira:E11:0"),
            text=(
                "## 환자수·비용\n"
                "| 상병코드 | 상병명 | 성별 | 연령대 | 환자수 |\n"
                "| --- | --- | --- | --- | ---: |\n"
                "| E10 | 1형 당뇨병 | 남 | 0~9세 | 350 |\n"
                "| E10 | 1형 당뇨병 | 남 | 10~19세 | 1,601 |\n"
                "| E11 | 2형 당뇨병 | 남 | 0~9세 | 111 |"
            ),
        )
    )
    commentary = (
        "## 핵심 답\n"
        "2025년 E10 남 · 0~9세 환자수 350명으로 확인되었습니다. [출처: 건강보험심사평가원]\n"
        "2025년 E10 남 · 10~19세 환자수 1,601명으로 확인되었습니다. [출처: 건강보험심사평가원]\n"
        "2025년 E11 남 · 0~9세 환자수 111명으로 확인되었습니다. [출처: 건강보험심사평가원]\n\n"
        "연령대별 차이는 표에서 확인할 수 있습니다."
    )

    composed = compose_lossless_answer(
        rendered,
        commentary,
        synthesis_trace={"status": "synthesized"},
        mode="inject",
        question="성별, 나이 기준으로도 알려줘 환자수",
    )

    assert composed.text.count("## 핵심 답") == 1
    assert composed.text.count("환자수 350명으로 확인되었습니다") == 0
    assert composed.text.count("| E10 | 1형 당뇨병 | 남 | 0~9세 | 350 |") == 1
    assert "연령대별 차이는 표에서 확인할 수 있습니다." in composed.text
    assert composed.trace["homogeneous_patient_narratives_promoted"] == 3


def test_patient_table_does_not_remove_nonhomogeneous_commentary() -> None:
    rendered = _rendered(
        RenderNode(
            block_id="hira-statistics:records",
            record_ids=("hira:E10:0",),
            text=(
                "## 환자수·비용\n"
                "| 상병코드 | 상병명 | 성별 | 연령대 | 환자수 |\n"
                "| --- | --- | --- | --- | ---: |\n"
                "| E10 | 1형 당뇨병 | 남 | 0~9세 | 350 |"
            ),
        )
    )
    commentary = (
        "## 핵심 답\n"
        "E10의 0~9세 수치는 표에서 확인할 수 있습니다.\n"
        "연령대별 수신 범위가 달라 전체 경향으로 일반화하지 않습니다.\n"
        "여성 자료는 이번 응답에 포함되지 않았습니다."
    )

    composed = compose_lossless_answer(
        rendered,
        commentary,
        synthesis_trace={"status": "synthesized"},
        mode="inject",
        question="성별, 나이 기준으로도 알려줘 환자수",
    )

    assert "E10의 0~9세 수치는 표에서 확인할 수 있습니다." in composed.text


def test_jw_product_top_ten_scopes_file_sql_to_jw_manufacturer() -> None:
    schema = {
        "logical_name": "doc:sheet",
        "columns": [
            {"query_name": "c1", "source_name": "MFR NAME KOR"},
            {"query_name": "c2", "source_name": "PRODUCT NAME KOR"},
            {"query_name": "c3", "source_name": "VALUES LC SI PRICE 1/2026"},
        ],
    }

    resolution = _resolve_deterministic_select("JW 제품 매출 상위 10개", (schema,))

    assert resolution.plan is not None
    assert "c1 = 'JW중외제약'" in resolution.plan["sql"]
    assert "GROUP BY c2" in resolution.plan["sql"]
    assert "LIMIT 10" in resolution.plan["sql"]


def test_generic_nouns_are_not_completion_entities() -> None:
    plan = _plan("JW 제품 매출 상위 10개").model_copy(
        update={
            "requested_answer_shape": _plan("JW 제품 매출 상위 10개")
            .requested_answer_shape.model_copy(
                update={
                    "entities": ("JW중외제약", "제품", "매출", "현황", "기준", "정보"),
                    "measure_or_attribute": ("sales",),
                }
            ),
            "answer_sources": ("mart",),
        }
    )

    assert _completion_entities(plan) == ("JW중외제약",)
