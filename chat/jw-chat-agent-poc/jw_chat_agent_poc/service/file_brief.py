from __future__ import annotations

from pathlib import Path

from jw_chat_agent_poc.service.file_search_client import UploadedFileOverview


def render_uploaded_file_machine_brief(overview: UploadedFileOverview) -> str:
    """Render observed upload metadata without treating it as answer evidence."""

    suffix = Path(overview.file_name).suffix.lower()
    lines = [f"### {overview.file_name}"]
    if overview.sql_tables:
        lines.append("- 유형: 정형 데이터")
        for table in overview.sql_tables:
            lines.append(
                f"- {table.sheet_name}: {table.row_count:,}행 x {table.column_count:,}열"
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
