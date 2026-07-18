from __future__ import annotations

from pathlib import Path
import re

from jw_chat_agent_poc.service.file_search_client import UploadedFileOverview


_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]+")
_MARKDOWN_INLINE_RE = re.compile(r"([\\`*{}\[\]()<>#+|])")


def safe_upload_markdown_inline(value: object, *, max_length: int = 160) -> str:
    """Render upload metadata as one bounded, inert markdown line."""

    collapsed = " ".join(_CONTROL_CHARS_RE.sub(" ", str(value or "")).split())
    bounded = collapsed[:max_length].rstrip()
    return _MARKDOWN_INLINE_RE.sub(r"\\\1", bounded)


def render_uploaded_file_machine_brief(overview: UploadedFileOverview) -> str:
    """Render observed upload metadata without treating it as answer evidence."""

    suffix = Path(overview.file_name).suffix.lower()
    lines = [f"### {safe_upload_markdown_inline(overview.file_name)}"]
    if overview.title:
        lines.append(f"- 제목: {safe_upload_markdown_inline(overview.title)}")
    worksheet_rows = overview.sql_tables or overview.sheets
    if worksheet_rows:
        lines.append("- 유형: 정형 데이터")
        if overview.sheet_count is not None:
            lines.append(f"- 시트: {overview.sheet_count:,}개")
        for table in worksheet_rows:
            sheet_name = safe_upload_markdown_inline(
                getattr(table, "sheet_name", None) or getattr(table, "name", "")
            )
            row_count = getattr(table, "row_count", None)
            column_count = getattr(table, "column_count", None)
            bounds = []
            if row_count is not None:
                bounds.append(f"{row_count:,}행")
            if column_count is not None:
                bounds.append(f"{column_count:,}열")
            lines.append(
                f"- {sheet_name}" + (f": {' x '.join(bounds)}" if bounds else "")
            )
        examples = (
            "이 파일의 전체 구조를 설명해줘",
            "시트별 주요 지표를 요약해줘",
            "원하는 기준별 합계를 알려줘",
        )
    else:
        type_name = {
            ".pdf": "PDF 문서",
            ".pptx": "발표 자료",
            ".docx": "문서",
        }.get(suffix, "업로드 문서")
        lines.append(f"- 유형: {type_name}")
        if overview.page_count is not None:
            lines.append(f"- 페이지: {overview.page_count:,}쪽")
        if overview.slide_count is not None:
            lines.append(f"- 슬라이드: {overview.slide_count:,}장")
        if overview.chunk_count:
            lines.append(f"- 검색 가능한 내용 조각: {overview.chunk_count:,}개")
        examples = (
            "이 문서의 핵심 내용을 요약해줘",
            "결론이 무엇인지 알려줘",
            "특정 주제가 어디에 나오는지 찾아줘",
        )
    lines.append("- 예시 질문:")
    lines.extend(f"  - {example}" for example in examples)
    return "\n".join(lines)
