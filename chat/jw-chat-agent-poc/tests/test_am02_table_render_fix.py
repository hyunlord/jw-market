from __future__ import annotations

from jw_chat_agent_poc.service.markdown_cleanup import cleanup_markdown_answer
from jw_chat_agent_poc.service.runtime_provenance import _table_cell_count_issues


AM02_BROKEN_MARKDOWN = """\
시장 내 주요 브랜드의 점유율을 살펴보면, 로수젯이 9.13%로 1위입니다. 리피토가 6.13%로 2위입니다.
| 항목 | 값 |
| --- | --- |
| 브랜드/시장 | 리바로 리바로젯 |
| 기간 | 2026-05 |
| 시장규모 | 2,139.25억원 |
| HHI | 253.6207 |
| 순위 | 구분 | 시장점유율 | 매출 |
| --- | --- | --- | --- |
| 1 | 로수젯 | 9.13% | 195.24억원 |
| 2 | 리피토 | 6.13% | 131.09억원 |"""


def test_am02_cleanup_separates_adjacent_tables_with_different_widths() -> None:
    cleaned = cleanup_markdown_answer(AM02_BROKEN_MARKDOWN)

    assert cleaned.startswith("시장 내 주요 브랜드의 점유율")
    assert "| 브랜드/시장 | 리바로 리바로젯 |" in cleaned
    assert "| HHI | 253.6207 |\n\n| 순위 | 구분 | 시장점유율 | 매출 |" in cleaned
    assert _table_cell_count_issues(cleaned) == []


def test_am02_cleanup_preserves_valid_table_bytes() -> None:
    valid = """\
| 항목 | 값 |
| --- | --- |
| 기간 | 2026-05 |
| 시장규모 | 2,139.25억원 |"""

    assert cleanup_markdown_answer(valid) == valid
