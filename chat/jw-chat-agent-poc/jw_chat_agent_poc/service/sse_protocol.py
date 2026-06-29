from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Final, Iterator, Literal

from jw_chat_agent_poc.service.answer_safety import chunk_text


_TABLE_DIVIDER_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*\|\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|\s*$"
)


@dataclass(frozen=True, slots=True)
class MarkdownSegment:
    kind: Literal["prose", "table"]
    text: str


def iter_markdown_sse_events(markdown: str) -> Iterator[str]:
    for segment in _markdown_segments(markdown):
        match segment.kind:
            case "prose":
                yield from (_sse_delta(token) for token in chunk_text(segment.text))
            case "table":
                yield _sse_json_event("markdown_block", {"kind": "table", "markdown": _table_markdown(segment.text)})


def _markdown_segments(markdown: str) -> Iterator[MarkdownSegment]:
    lines = markdown.splitlines(keepends=True)
    prose_lines: list[str] = []
    index = 0
    while index < len(lines):
        if _is_table_start(lines, index):
            if prose_lines:
                yield MarkdownSegment(kind="prose", text="".join(prose_lines))
                prose_lines = []
            table_lines: list[str] = []
            while index < len(lines) and _is_table_row(lines[index]):
                table_lines.append(lines[index])
                index += 1
            yield MarkdownSegment(kind="table", text="".join(table_lines))
            continue
        prose_lines.append(lines[index])
        index += 1
    if prose_lines:
        yield MarkdownSegment(kind="prose", text="".join(prose_lines))


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
