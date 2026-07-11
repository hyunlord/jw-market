from __future__ import annotations

from jw_chat_agent_poc.orchestrator.answer_facts import answer_fact_markdown
from jw_chat_agent_poc.orchestrator.unavailable_response import sanitize_internal_diagnostics
from jw_chat_agent_poc.service.answer_safety import _sentence_from_evidence
from jw_chat_agent_poc.service.markdown_cleanup import cleanup_markdown_answer


def test_cleanup_blocks_internal_output_labels_without_changing_values() -> None:
    answer = (
        "리바로 시장 점유율은 9.17%이고 상위 5개 합계는 30.33%입니다.\n\n"
        "출처: ml_006 / strategy_006 / cd_008 / tool_call_1 / series / 확정 시장"
    )

    revised = cleanup_markdown_answer(answer)

    for internal in ("ml_", "strategy_", "cd_", "tool_call_", "확정 시장"):
        assert internal not in revised
    assert "series" not in revised
    assert "9.17%" in revised
    assert "30.33%" in revised


def test_cleanup_preserves_public_market_name() -> None:
    answer = "출처: 리바로/리바로젯 미확정 시장 · 2026-04 · 30.33%"

    assert cleanup_markdown_answer(answer) == answer


def test_answer_facts_prefers_public_market_and_omits_generated_tool_id() -> None:
    calls = [
        {
            "tool_name": "get_brand_series",
            "source": "UBIST",
            "render_data": {
                "period": "2026-04",
                "market_id": "ml_006",
                "market_name": "리바로/리바로젯 시장",
                "requested_axis": "series",
                "sales_억원": 870.2,
            },
        }
    ]

    markdown = answer_fact_markdown(calls, [])

    assert "리바로/리바로젯 시장" in markdown
    assert "시계열" in markdown
    assert "ml_006" not in markdown
    assert "tool_call_" not in markdown
    assert "870.2" in markdown


def test_unavailable_cleanup_does_not_restore_intentional_internal_ids() -> None:
    answer = (
        "- 데이터 상세: UBIST - 기간 2025-07~2026-04, 시장: ml_006 "
        "(market_landscape, 분모 470), 참고: strategy_006 기준 순위는 6/516"
    )

    revised = sanitize_internal_diagnostics(answer)

    assert "ml_006" not in revised
    assert "strategy_006" not in revised
    assert "분모 470" in revised
    assert "6/516" in revised


def test_cleanup_fixes_narrow_korean_typos() -> None:
    assert cleanup_markdown_answer("관련 자료가 없은 상태입니다.") == "관련 자료가 없는 상태입니다."


def test_sentence_from_evidence_does_not_create_confirmed_is_grammar_error() -> None:
    evidence = "상위 브랜드 최신 점유율과 매출 순위가 확인되었습니다"

    assert _sentence_from_evidence(evidence) == f"{evidence}."


def test_cleanup_removes_non_adjacent_duplicate_prose_once() -> None:
    repeated = "상위 브랜드의 점유율 이동을 함께 확인했습니다."
    answer = f"{repeated}\n\n중간에 다른 해석이 있습니다.\n\n{repeated}"

    revised = cleanup_markdown_answer(answer)

    assert revised.count(repeated) == 1
    assert "중간에 다른 해석이 있습니다." in revised


def test_cleanup_is_idempotent() -> None:
    answer = "시장 ml_006의 값은 30.33%입니다.\n\n시장 ml_006의 값은 30.33%입니다."

    once = cleanup_markdown_answer(answer)

    assert cleanup_markdown_answer(once) == once
