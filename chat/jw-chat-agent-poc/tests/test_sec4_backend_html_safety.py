from __future__ import annotations

import pytest

from jw_chat_agent_poc.service.markdown_cleanup import cleanup_markdown_answer


@pytest.mark.parametrize(
    ("markdown", "forbidden"),
    (
        ("<script>alert(1)</script>", ("<script", "alert(1)")),
        ("<iframe src=https://example.test>fallback</iframe>", ("<iframe", "fallback")),
        ("<object data=https://example.test>fallback</object>", ("<object", "fallback")),
        ("<embed src=https://example.test>", ("<embed",)),
        ("<img src=x onerror=alert(1)>", ("onerror",)),
        ('<a href="javascript:alert(1)">link</a>', ("javascript:",)),
        ("[link](javascript:alert(1))", ("javascript:",)),
        ('<img src="data:text/html,<script>alert(1)</script>">', ("data:text/html", "<script")),
    ),
)
def test_cleanup_neutralizes_executable_markup(markdown: str, forbidden: tuple[str, ...]) -> None:
    cleaned = cleanup_markdown_answer(markdown)

    for token in forbidden:
        assert token.casefold() not in cleaned.casefold()


def test_cleanup_preserves_safe_markdown_byte_for_byte() -> None:
    markdown = (
        "## 시장 요약\n\n"
        "| 브랜드 | 매출 |\n"
        "| --- | --- |\n"
        "| 리바로 | 80억원 |\n\n"
        "- **근거:** [공식 출처](https://example.test/report)\n"
        "- 조건: 매출 < 100억원"
    )

    assert cleanup_markdown_answer(markdown) == markdown
