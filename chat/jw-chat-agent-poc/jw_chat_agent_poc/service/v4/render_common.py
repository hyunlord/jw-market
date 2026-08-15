from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
import re
from typing import Any

from jw_chat_agent_poc.service.v4.lossless_contracts import CoverageLedger


def coverage_text(coverage: CoverageLedger, *, rendered: int) -> str:
    total = str(coverage.total_reported) if coverage.total_reported is not None else "확인 불가"
    return (
        f"원천 검색 {total}건 · 수신 {coverage.records_received}건 · "
        f"중복 제거 후 {coverage.records_unique}건 · 상세 표시 {rendered}건"
    )


def table(headers: Sequence[str], rows: Iterable[Sequence[object]]) -> str:
    rendered_rows = [[cell(value) for value in row] for row in rows]
    if not rendered_rows:
        return ""
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rendered_rows)
    return "\n".join(lines)


def cell(value: object) -> str:
    rendered = str(value).strip()
    if not rendered:
        return "원천 미제공"
    return (
        rendered.replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("_", "\\_")
        .replace("*", "\\*")
        .replace("`", "\\`")
        .replace("\n", "<br>")
    )


def display(value: object) -> str:
    return text(value) or "원천 미제공"


def list_display(value: object, *, na: str = "원천 미제공") -> str:
    if not isinstance(value, (list, tuple)):
        return display(value)
    values = [text(item) for item in value if text(item)]
    return ", ".join(values) if values else na


def results_display(value: object) -> str:
    if value is True:
        return "결과 게시"
    if value is False:
        return "결과 미게시"
    return "원천 미제공"


def enrollment_display(value: object) -> str:
    if not isinstance(value, Mapping):
        return "원천 미제공"
    count = value.get("count")
    kind = text(value.get("type"))
    if count is None and not kind:
        return "원천 미제공"
    return " ".join(part for part in (str(count) if count is not None else "", kind) if part)


def effective_date(payload: Mapping[str, Any], raw: str) -> str:
    direct = text(payload.get("source_date"))
    if direct:
        return direct
    match = re.search(r"시행일(?:자)?\s*[:：]?\s*((?:19|20)\d{2}[-./]\d{1,2}[-./]\d{1,2})", raw)
    return match.group(1) if match else "원천 미제공"


def link(payload: Mapping[str, Any]) -> str:
    title = display(payload.get("title"))
    url = text(payload.get("url"))
    return f"[{title}]({url})" if url else title


def text(value: object) -> str:
    return str(value).strip() if value not in (None, "") else ""
