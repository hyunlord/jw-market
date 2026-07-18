from __future__ import annotations

import json
import re
from dataclasses import asdict
from typing import Any

from jw_chat_agent_poc.service.file_brief import safe_upload_markdown_inline
from jw_chat_agent_poc.service.file_search_client import (
    UploadedFileOverview,
    UploadedSqlTableOverview,
    UploadedWorksheetOverview,
)


_NUMBER_RE = re.compile(r"(?<![A-Za-z])\d[\d,]*(?:\.\d+)?")


class FileBriefValidationError(ValueError):
    """Raised when a generated upload brief exceeds observed metadata."""


def serialize_file_overviews(overviews: tuple[UploadedFileOverview, ...]) -> list[dict[str, Any]]:
    return [asdict(overview) for overview in overviews]


def deserialize_file_overviews(items: object) -> tuple[UploadedFileOverview, ...]:
    if not isinstance(items, list):
        return ()
    overviews: list[UploadedFileOverview] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        file_name = str(item.get("file_name") or "").strip()
        if not file_name:
            continue
        sql_tables = tuple(
            UploadedSqlTableOverview(
                sheet_name=str(table.get("sheet_name") or ""),
                row_count=max(0, int(table.get("row_count") or 0)),
                column_count=max(0, int(table.get("column_count") or 0)),
            )
            for table in item.get("sql_tables", [])
            if isinstance(table, dict) and str(table.get("sheet_name") or "").strip()
        )
        sheets = tuple(
            UploadedWorksheetOverview(
                name=str(sheet.get("name") or ""),
                row_count=_optional_int(sheet.get("row_count")),
                column_count=_optional_int(sheet.get("column_count")),
            )
            for sheet in item.get("sheets", [])
            if isinstance(sheet, dict) and str(sheet.get("name") or "").strip()
        )
        overviews.append(
            UploadedFileOverview(
                file_name=file_name,
                storage_route=str(item.get("storage_route") or "vdb"),
                chunk_count=max(0, int(item.get("chunk_count") or 0)),
                sql_tables=sql_tables,
                title=_optional_text(item.get("title")),
                sheet_count=_optional_int(item.get("sheet_count")),
                sheets=sheets,
                page_count=_optional_int(item.get("page_count")),
                slide_count=_optional_int(item.get("slide_count")),
            )
        )
    return tuple(overviews)


def build_file_brief_messages(overviews: tuple[UploadedFileOverview, ...]) -> list[dict[str, str]]:
    observed = [
        {
            **item,
            "allowed_suggested_questions": list(_allowed_suggested_questions(overview)),
        }
        for item, overview in zip(serialize_file_overviews(overviews), overviews, strict=True)
    ]
    return [
        {
            "role": "system",
            "content": (
                "업로드 직후 탐색용 파일 브리프를 JSON으로만 작성한다. "
                "각 파일의 allowed_suggested_questions 중 서로 다른 3개를 골라 suggested_questions에 그대로 쓴다. "
                "후보 문구를 수정하거나 새 질문을 만들지 않는다. "
                "입력 파일을 빠뜨리거나 합치지 않고 다른 필드를 추가하지 않는다. "
                "출력 형식은 {\"files\":[{\"file_name\":str,"
                "\"suggested_questions\":[str,str,str]}]} 이다."
            ),
        },
        {
            "role": "user",
            "content": json.dumps({"observed_files": observed}, ensure_ascii=False),
        },
    ]


def parse_and_render_file_briefs(
    raw_text: str,
    overviews: tuple[UploadedFileOverview, ...],
) -> str:
    if not overviews:
        raise FileBriefValidationError("no observed files")
    payload = _parse_json_object(raw_text)
    files = payload.get("files")
    if not isinstance(files, list) or len(files) != len(overviews):
        raise FileBriefValidationError("brief file count does not match observed files")

    expected_names = [overview.file_name for overview in overviews]
    actual_names = [str(item.get("file_name") or "") for item in files if isinstance(item, dict)]
    if actual_names != expected_names:
        raise FileBriefValidationError("brief file names or order do not match")

    rendered: list[str] = []
    for item, overview in zip(files, overviews, strict=True):
        if not isinstance(item, dict):
            raise FileBriefValidationError("brief item is not an object")
        if set(item) != {"file_name", "suggested_questions"}:
            raise FileBriefValidationError("brief item contains unsupported fields")
        questions = _string_list(item.get("suggested_questions"))
        if len(questions) != 3:
            raise FileBriefValidationError("brief must contain exactly three suggested questions")
        if len(set(questions)) != 3:
            raise FileBriefValidationError("brief questions must be distinct")
        if _NUMBER_RE.search(" ".join(questions)):
            raise FileBriefValidationError("brief questions must not introduce numeric claims")
        allowed_questions = set(_allowed_suggested_questions(overview))
        if not set(questions).issubset(allowed_questions):
            raise FileBriefValidationError("brief questions must use the safe observed-file templates")
        safe_file_name = safe_upload_markdown_inline(item["file_name"])
        rendered.extend(
            [
                f"#### 파일 브리프 - {safe_file_name}",
                " ".join(_grounded_summary(overview)),
                "추천 질문:",
                *(f"- {safe_upload_markdown_inline(question)}" for question in questions),
            ]
        )
    return "\n\n".join(rendered)


def render_file_brief_grounding_text(
    overviews: tuple[UploadedFileOverview, ...],
) -> str:
    """Render only deterministic statements derived from observed upload metadata."""

    return "\n".join(
        line
        for overview in overviews
        for line in _grounded_summary(overview)
    )


def _parse_json_object(raw_text: str) -> dict[str, Any]:
    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        payload = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise FileBriefValidationError("brief is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise FileBriefValidationError("brief root must be an object")
    return payload


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    items = [str(item).strip() for item in value if str(item).strip()]
    return items if len(items) == len(value) else []


def _allowed_suggested_questions(overview: UploadedFileOverview) -> tuple[str, ...]:
    suffix = overview.file_name.rsplit(".", 1)[-1].lower() if "." in overview.file_name else ""
    if overview.sql_tables or overview.sheets or suffix in {"xlsx", "xlsm", "csv"}:
        return (
            "이 파일의 전체 구조를 설명해줘",
            "시트별 주요 지표를 요약해줘",
            "원하는 기준별 합계를 알려줘",
            "데이터의 기간 범위를 알려줘",
            "상위 항목을 집계해줘",
        )
    if suffix == "pptx":
        return (
            "이 발표 자료의 주요 내용을 요약해줘",
            "슬라이드 구성을 설명해줘",
            "특정 주제가 있는지 찾아줘",
            "결론이나 제안이 있는지 확인해줘",
            "표나 수치가 있는 슬라이드를 찾아줘",
        )
    if suffix == "docx":
        return (
            "이 문서의 주요 내용을 요약해줘",
            "문서의 섹션 구성을 설명해줘",
            "특정 주제가 있는지 찾아줘",
            "결론이나 제안이 있는지 확인해줘",
            "표나 수치가 있는지 확인해줘",
        )
    return (
        "이 문서의 주요 내용을 요약해줘",
        "문서의 구성을 설명해줘",
        "특정 주제가 있는지 찾아줘",
        "결론이나 제안이 있는지 확인해줘",
        "표나 수치가 있는 페이지를 찾아줘",
    )


def _grounded_summary(overview: UploadedFileOverview) -> tuple[str, ...]:
    suffix = overview.file_name.rsplit(".", 1)[-1].upper() if "." in overview.file_name else "파일"
    file_name = safe_upload_markdown_inline(overview.file_name)
    lines = [f"{file_name}은 {safe_upload_markdown_inline(suffix)} 형식의 업로드 파일입니다."]
    if overview.title:
        lines.append(f"관측된 제목은 '{safe_upload_markdown_inline(overview.title)}'입니다.")
    if overview.sheet_count is not None:
        lines.append(f"시트 {overview.sheet_count:,}개가 확인됩니다.")
    if overview.sheets:
        sheet = overview.sheets[0]
        shape = [
            value
            for value in (
                f"{sheet.row_count:,}행" if sheet.row_count is not None else "",
                f"{sheet.column_count:,}열" if sheet.column_count is not None else "",
            )
            if value
        ]
        observed_shape = f"의 범위는 {' x '.join(shape)}입니다." if shape else "가 확인됩니다."
        lines.append(f"첫 시트 '{safe_upload_markdown_inline(sheet.name)}'{observed_shape}")
    if overview.page_count is not None:
        lines.append(f"전체 {overview.page_count:,}페이지가 관측됩니다.")
    if overview.slide_count is not None:
        lines.append(f"전체 {overview.slide_count:,}슬라이드가 관측됩니다.")
    lines.append("세부 내용과 수치 답변은 실제 파일 검색 결과를 근거로 확인합니다.")
    if len(lines) < 3:
        lines.append("이 파일을 기준으로 후속 질문을 이어갈 수 있습니다.")
    return tuple(lines[:5])


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None
