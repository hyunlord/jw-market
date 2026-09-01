from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final, Literal

from jw_chat_agent_poc.service.answer_safety import chunk_text

_TABLE_DIVIDER_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*\|\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|\s*$"
)
_ATX_HEADING_RE: Final[re.Pattern[str]] = re.compile(r"^\s*#{1,6}\s+\S")
_BOLD_HEADING_RE: Final[re.Pattern[str]] = re.compile(r"^\s*\*\*[^*\n]+\*\*\s*$")
_LIMIT_NOTICE_RE: Final[re.Pattern[str]] = re.compile(
    r"전체\s+(?P<total>[\d,]+)건\s+중\s+(?P<shown>[\d,]+)건\s+표시"
)
_PATENT_TABLE_CAPTION_RE: Final[re.Pattern[str]] = re.compile(
    r"^제품특허 조합 [\d,]+건 · 특허번호 [\d,]+건 · 표는 특허 단위 [\d,]+행"
)
_PATENT_AGGREGATE_CAPTION_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:국내 특허 원천 [\d,]+건 기준|제품특허 [\d,]+건 기준)$"
)
_HIRA_COST_CAPTION_RE: Final[re.Pattern[str]] = re.compile(
    r"^보험자부담금은 .+년 기준만 원천 제공되며 .+년 행은 '-'로 표시했습니다\.$"
)
_NUMBER_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?P<number>-?[\d,]+(?:\.\d+)?)\s*(?P<unit>억원|백만원|만원|원|명|건|%)?\s*$"
)
_DATE_LABELS: Final[tuple[str, ...]] = ("연도", "기간", "날짜", "일자", "만료일")
_NUMBER_LABELS: Final[tuple[str, ...]] = (
    "금액",
    "매출",
    "점유율",
    "환자수",
    "환자 수",
    "건수",
    "수량",
    "비율",
    "순위",
)


@dataclass(frozen=True, slots=True)
class MarkdownSegment:
    kind: Literal["prose", "table"]
    text: str


def iter_markdown_sse_events(
    markdown: str,
    *,
    emit_legacy_table_blocks: bool = True,
) -> Iterator[str]:
    segments = list(_markdown_segments(markdown))
    table_index = 0
    structured_tables: list[dict[str, object]] = []
    for index, segment in enumerate(segments):
        match segment.kind:
            case "prose":
                yield from (_sse_delta(token) for token in chunk_text(segment.text))
            case "table":
                following_text = segments[index + 1].text if index + 1 < len(segments) else ""
                structured_table = _structured_table_payload(
                    segment.text,
                    table_index=table_index,
                    following_text=following_text,
                )
                if structured_table is not None:
                    structured_tables.append(structured_table)
                    yield _sse_json_event("tables", structured_tables)
                if emit_legacy_table_blocks or structured_table is None:
                    yield _sse_json_event(
                        "markdown_block",
                        {"kind": "table", "markdown": _table_markdown(segment.text)},
                    )
                table_index += 1


def strip_structured_markdown_tables(markdown: str) -> str:
    """Remove only tables that can be represented by the structured table event."""

    segments = list(_markdown_segments(_reflow_inline_markdown_tables(markdown)))
    retained: list[str] = []
    for index, segment in enumerate(segments):
        if segment.kind == "prose":
            retained.append(segment.text)
            continue
        following_text = segments[index + 1].text if index + 1 < len(segments) else ""
        if _structured_table_payload(
            segment.text,
            table_index=index,
            following_text=following_text,
        ) is None:
            retained.append(segment.text)
    return "\n\n".join(part.strip() for part in retained if part.strip()).strip()


def _reflow_inline_markdown_tables(markdown: str) -> str:
    """Restore line boundaries lost when a structured answer paragraph is flattened."""

    if not re.search(r"\|\s*\|\s*:?-{3,}", markdown):
        return markdown
    lines = re.sub(r"\|\s*\|\s*(?=[^|\n])", "|\n| ", markdown).splitlines()
    index = 1
    while index < len(lines):
        divider = lines[index]
        if not _is_table_divider(divider):
            index += 1
            continue
        column_count = len(_split_table_row(divider))
        header_parts = re.split(r"(?<!\\)\|", lines[index - 1].rstrip().rstrip("|"))
        if not lines[index - 1].lstrip().startswith("|") and len(header_parts) > column_count:
            prefix = "|".join(header_parts[:-column_count]).rstrip()
            header = [part.strip() for part in header_parts[-column_count:]]
            lines[index - 1 : index] = [prefix, f"| {' | '.join(header)} |"]
            index += 1
        row_index = index + 1
        while row_index < len(lines) and lines[row_index].lstrip().startswith("|"):
            row_parts = _split_table_row(lines[row_index])
            if len(row_parts) <= column_count:
                row_index += 1
                continue
            table_cells = row_parts[:column_count]
            trailing = " | ".join(row_parts[column_count:]).strip()
            lines[row_index : row_index + 1] = [
                f"| {' | '.join(table_cells)} |",
                trailing,
            ]
            break
        index = row_index + 1
    return "\n".join(line for line in lines if line)


def _structured_table_payload(
    text: str,
    *,
    table_index: int,
    following_text: str,
) -> dict[str, object] | None:
    lines = text.strip().splitlines()
    divider_index = next(
        (index for index, line in enumerate(lines) if _is_table_divider(line)),
        None,
    )
    if divider_index is None or divider_index == 0:
        return None
    header = _split_table_row(lines[divider_index - 1])
    raw_rows = [
        _split_table_row(line)
        for line in lines[divider_index + 1 :]
        if _is_table_row(line)
    ]
    if not header or any(len(row) != len(header) for row in raw_rows):
        return None
    prefix_lines = lines[: divider_index - 1]
    title = _table_title(prefix_lines)
    caption = _table_caption(prefix_lines)
    column_types = tuple(_column_type(label, raw_rows, index) for index, label in enumerate(header))
    rows = [
        [_typed_cell(value, column_types[index]) for index, value in enumerate(row)]
        for row in raw_rows
    ]
    notice = _LIMIT_NOTICE_RE.search(following_text)
    total_rows = int(notice.group("total").replace(",", "")) if notice else len(rows)
    table_identity = f"{table_index}\n{title}\n{'|'.join(header)}"
    table_hash = hashlib.sha256(table_identity.encode("utf-8")).hexdigest()[:12]
    table_id = f"table-{table_hash}"
    column_units = tuple(
        _column_unit(raw_rows, index, column_types[index])
        for index in range(len(header))
    )
    columns = [
        {
            "key": f"column_{index + 1}",
            "label": label,
            "type": column_types[index],
            "unit": column_units[index],
            "align": (
                "right"
                if column_types[index] == "number"
                else "center" if column_types[index] == "date" else "left"
            ),
        }
        for index, label in enumerate(header)
    ]
    source_lane = _source_lane(title, header)
    payload: dict[str, object] = {
        "table_id": table_id,
        "title": title,
        "source_label": _source_label(source_lane),
        "columns": columns,
        "rows": [
            {
                "cells": {
                    column["key"]: value
                    for column, value in zip(columns, row, strict=True)
                },
                "record_id": f"{table_id}:row-{row_index + 1}",
            }
            for row_index, row in enumerate(rows)
        ],
        "row_count": len(rows),
        "omitted_columns": [],
        "unit": _table_unit(raw_rows, column_types),
        "source_lane": source_lane,
        "truncated": total_rows > len(rows),
        "total_rows": total_rows,
    }
    if notice:
        notice_line = next(
            line.strip()
            for line in following_text.splitlines()
            if _LIMIT_NOTICE_RE.search(line)
        )
        caption = " · ".join(filter(None, (caption, notice_line)))
    if caption:
        payload["caption"] = caption
    return payload


def _column_unit(rows: list[list[str]], column_index: int, column_type: str) -> str:
    if column_type != "number":
        return ""
    units = {
        matched.group("unit")
        for row in rows
        if column_index < len(row)
        and (matched := _NUMBER_RE.match(row[column_index])) is not None
        and matched.group("unit")
    }
    return next(iter(units)) if len(units) == 1 else ""


def _source_label(source_lane: str) -> str:
    return {
        "hira": "건강보험심사평가원",
        "patent": "식품의약품안전처 의약품 특허목록",
        "clinicaltrials": "ClinicalTrials.gov",
        "mart": "내부 데이터마트",
        "file_sql": "업로드 문서(표 집계)",
        "unknown": "조회 결과",
    }.get(source_lane, source_lane)


def _split_table_row(line: str) -> list[str]:
    return [cell.strip().replace(r"\|", "|") for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))]


def _table_title(lines: list[str]) -> str:
    heading = next(
        (
            line.strip()
            for line in reversed(lines)
            if line.strip() and _is_table_heading(line)
        ),
        "표",
    )
    heading = re.sub(r"^#{1,6}\s+", "", heading)
    return heading.strip("* ") or "표"


def _table_caption(lines: list[str]) -> str:
    return next(
        (
            line.strip()
            for line in reversed(lines)
            if _is_patent_table_caption(line)
        ),
        "",
    )


def _column_type(label: str, rows: list[list[str]], column_index: int) -> str:
    if any(token in label for token in _DATE_LABELS):
        return "date"
    if any(token in label for token in _NUMBER_LABELS):
        return "number"
    values = [row[column_index] for row in rows if column_index < len(row)]
    if values and all(_NUMBER_RE.match(value) for value in values):
        return "number"
    return "string"


def _typed_cell(value: str, column_type: str) -> str | int | float | None:
    if column_type != "number":
        return value
    matched = _NUMBER_RE.match(value)
    if matched is None:
        return None
    numeric = matched.group("number").replace(",", "")
    return float(numeric) if "." in numeric else int(numeric)


def _table_unit(rows: list[list[str]], column_types: tuple[str, ...]) -> str:
    units = {
        matched.group("unit")
        for row in rows
        for index, value in enumerate(row)
        if index < len(column_types)
        and column_types[index] == "number"
        and (matched := _NUMBER_RE.match(value)) is not None
        and matched.group("unit")
    }
    return next(iter(units)) if len(units) == 1 else ""


def _source_lane(title: str, header: list[str]) -> str:
    context = f"{title} {' '.join(header)}".casefold()
    if any(token in context for token in ("업로드", "문서", "시트", "셀")):
        return "file_sql"
    if any(token in context for token in ("환자", "상병", "입원", "외래", "hira")):
        return "hira"
    if any(token in context for token in ("특허", "patent")):
        return "patent"
    if any(token in context for token in ("임상", "nct", "clinical")):
        return "clinicaltrials"
    if any(token in context for token in ("매출", "점유율", "시장", "브랜드")):
        return "mart"
    return "unknown"


def _markdown_segments(markdown: str) -> Iterator[MarkdownSegment]:
    lines = markdown.splitlines(keepends=True)
    prose_lines: list[str] = []
    index = 0
    while index < len(lines):
        if _is_table_start(lines, index):
            heading, prose_lines = _detach_table_heading(prose_lines)
            if prose_lines:
                prose = "".join(prose_lines)
                yield MarkdownSegment(kind="prose", text=prose.rstrip("\n") if heading else prose)
                prose_lines = []
            table_lines: list[str] = []
            while index < len(lines) and _is_table_row(lines[index]):
                table_lines.append(lines[index])
                index += 1
            yield MarkdownSegment(kind="table", text=f"{heading}{''.join(table_lines)}")
            continue
        prose_lines.append(lines[index])
        index += 1
    if prose_lines:
        yield MarkdownSegment(kind="prose", text="".join(prose_lines))


def _detach_table_heading(prose_lines: list[str]) -> tuple[str, list[str]]:
    heading_index = len(prose_lines) - 1
    while heading_index >= 0 and not prose_lines[heading_index].strip():
        heading_index -= 1
    if heading_index < 0:
        return "", prose_lines
    first_heading_index = heading_index
    if _is_patent_table_caption(prose_lines[heading_index]):
        # A caption may have a local heading and status preamble, but it must
        # never reach across a paragraph boundary and consume the answer
        # section heading as part of the atomic table event.
        for candidate in range(heading_index - 1, -1, -1):
            if not prose_lines[candidate].strip():
                break
            if _is_table_heading(prose_lines[candidate]):
                first_heading_index = candidate
                break
    if not (
        _is_table_heading(prose_lines[first_heading_index])
        or (
            first_heading_index == heading_index
            and _is_patent_table_caption(prose_lines[first_heading_index])
        )
    ):
        return "", prose_lines
    heading = "".join(prose_lines[first_heading_index:])
    remaining = prose_lines[:first_heading_index]
    while remaining and not remaining[-1].strip():
        remaining.pop()
    return heading, remaining


def _is_patent_table_caption(line: str) -> bool:
    stripped = line.strip()
    return bool(
        _PATENT_TABLE_CAPTION_RE.match(stripped)
        or _PATENT_AGGREGATE_CAPTION_RE.match(stripped)
        or _HIRA_COST_CAPTION_RE.match(stripped)
    )


def _is_table_heading(line: str) -> bool:
    stripped = line.strip()
    return bool(_ATX_HEADING_RE.match(stripped) or _BOLD_HEADING_RE.match(stripped))


def _is_table_start(lines: list[str], index: int) -> bool:
    return _is_table_row(lines[index]) and index + 1 < len(lines) and _is_table_divider(lines[index + 1])


def _is_table_row(line: str) -> bool:
    return line.lstrip().startswith("|")


def _is_table_divider(line: str) -> bool:
    return bool(_TABLE_DIVIDER_RE.match(line.strip()))


def _table_markdown(text: str) -> str:
    return f"\n\n{text.strip()}\n\n"


def _sse_delta(token: str) -> str:
    lines = token.split("\n")
    data = "\n".join(f"data: {line}" for line in lines)
    return f"event: delta\n{data}\n\n"


def _sse_json_event(event_name: str, payload: object) -> str:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event_name}\ndata: {data}\n\n"
