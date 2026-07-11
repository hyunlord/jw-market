from __future__ import annotations

import logging
import time

from jw_chat_agent_poc.common import timing
from jw_chat_agent_poc.orchestrator.answer_facts import answer_fact_markdown
from jw_chat_agent_poc.service.markdown_cleanup import cleanup_markdown_answer


def test_cleanup_deduplicates_non_adjacent_bullets() -> None:
    repeated = "- 경쟁 구도 변화는 점유율 이동 관점에서 해석해야 합니다."
    raw = "\n".join((repeated, "- 브랜드별 추이는 서로 다릅니다.", repeated))

    cleaned = cleanup_markdown_answer(raw)

    assert cleaned.count(repeated) == 1


def test_cleanup_removes_heading_with_only_separator_before_next_heading() -> None:
    raw = "### 상위 브랜드 추이\n\n---\n\n### 결론\n\n확정된 수치를 유지합니다."

    cleaned = cleanup_markdown_answer(raw)

    assert "### 상위 브랜드 추이" not in cleaned
    assert "### 결론" in cleaned
    assert "확정된 수치를 유지합니다." in cleaned


def test_answer_fact_markdown_deduplicates_identical_call_blocks() -> None:
    call = {
        "tool": "get_brand_metric",
        "source": "cache",
        "render_data": {
            "brand": "테스트브랜드",
            "metric": "sales",
            "period": "2026-04",
            "sales_억원": 84.93,
        },
    }

    rendered = answer_fact_markdown([call, call], ["cache"])

    assert rendered.count("### 테스트브랜드 지표 fact") == 1


def test_stage_timing_info_reaches_stdout_handler(capfd) -> None:
    with timing.stage({}, "answer_cleanup", "markdown cleanup"):
        pass
    for handler in timing.STAGE_TIMING_LOGGER.handlers:
        handler.flush()

    captured = capfd.readouterr()

    assert "stage_timing" in captured.out
    assert "answer_cleanup" in captured.out
    assert logging.INFO >= timing.STAGE_TIMING_LOGGER.level


def test_cleanup_large_markdown_completes_under_one_second() -> None:
    markdown = ("- 중복 없는 구조적 인사이트 문장입니다.\n" * 30_000)[:700_000]

    started = time.perf_counter()
    cleaned = cleanup_markdown_answer(markdown)
    elapsed = time.perf_counter() - started

    assert cleaned
    assert elapsed < 1.0
