from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final


@dataclass(frozen=True, slots=True)
class SseCapture:
    """Parsed operating SSE response for one chat question."""

    answer_markdown: str
    sources: str
    charts: tuple[dict[str, object], ...]
    timing: dict[str, object]
    steps: tuple[dict[str, object], ...]
    delta_count: int
    done_count: int
    error_count: int
    answer_chars: int
    render_issues: tuple[str, ...]


def parse_sse_file(path: Path) -> SseCapture:
    """Parse a raw Server-Sent Events file emitted by /chat/stream."""

    return parse_sse_text(path.read_text(encoding="utf-8"))


def parse_sse_text(raw_text: str) -> SseCapture:
    """Parse raw Server-Sent Events text emitted by /chat/stream."""

    events = _events(raw_text)
    answer_parts: list[str] = []
    charts: list[dict[str, object]] = []
    timing: dict[str, object] = {}
    steps: list[dict[str, object]] = []
    sources = ""
    delta_count = 0
    done_count = 0
    error_count = 0
    for event_name, data in events:
        match event_name:
            case "delta":
                delta_count += 1
                answer_parts.append(data)
            case "markdown_block":
                answer_parts.append(_markdown_block_item(data))
            case "sources":
                sources = data
            case "charts":
                charts.extend(_chart_items(data))
            case "timing":
                timing = _timing_item(data)
            case "step":
                item = _json_object(data)
                if item:
                    steps.append(item)
            case "done":
                done_count += 1
            case "error":
                error_count += 1
            case "conversation":
                continue
            case _:
                continue
    answer = "".join(answer_parts)
    render_issues = render_integrity_issues(answer, _naive_delta_text(raw_text))
    return SseCapture(
        answer_markdown=answer,
        sources=sources,
        charts=tuple(charts),
        timing=timing,
        steps=tuple(steps),
        delta_count=delta_count,
        done_count=done_count,
        error_count=error_count,
        answer_chars=len(answer),
        render_issues=render_issues,
    )


def _events(text: str) -> tuple[tuple[str, str], ...]:
    items: list[tuple[str, str]] = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        if not block.strip():
            continue
        name = "message"
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line.removeprefix("event:").strip()
            elif line.startswith("data:"):
                data_lines.append(line.removeprefix("data:").lstrip())
        items.append((name, "\n".join(data_lines)))
    return tuple(items)


def _chart_items(raw: str) -> tuple[dict[str, object], ...]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(item for item in parsed if isinstance(item, dict))


def _timing_item(raw: str) -> dict[str, object]:
    return _json_object(raw)


def _json_object(raw: str) -> dict[str, object]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _markdown_block_item(raw: str) -> str:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    if isinstance(parsed, dict) and isinstance(parsed.get("markdown"), str):
        return parsed["markdown"]
    return ""


def render_integrity_issues(answer: str, naive_delta_text: str = "") -> tuple[str, ...]:
    issues: list[str] = []
    for marker in _BROKEN_TABLE_SENTINELS:
        if marker in answer:
            issues.append(f"answer_table_join:{marker}")
        if marker in naive_delta_text:
            issues.append(f"naive_sse_table_join:{marker}")
    issues.extend(_table_cell_count_issues(answer))
    return tuple(issues)


def _naive_delta_text(raw_text: str) -> str:
    parts: list[str] = []
    current_event = "message"
    for line in raw_text.replace("\r\n", "\n").splitlines():
        if line.startswith("event:"):
            current_event = line.removeprefix("event:").strip()
            continue
        if current_event == "delta" and line.startswith("data:"):
            parts.append(line.removeprefix("data:").lstrip())
    return "".join(parts)


def _table_cell_count_issues(answer: str) -> tuple[str, ...]:
    issues: list[str] = []
    lines = answer.replace("\r\n", "\n").splitlines()
    index = 0
    while index < len(lines):
        if _is_table_start(lines, index):
            expected = _cell_count(lines[index])
            row_index = index + 1
            while row_index < len(lines) and _is_table_row(lines[row_index]):
                current = _cell_count(lines[row_index])
                if current != expected:
                    issues.append(f"table_cell_count:line={row_index + 1}:expected={expected}:actual={current}")
                row_index += 1
            index = row_index
            continue
        index += 1
    return tuple(issues)


def _is_table_start(lines: list[str], index: int) -> bool:
    return _is_table_row(lines[index]) and index + 1 < len(lines) and _TABLE_DIVIDER_RE.match(lines[index + 1].strip()) is not None


def _is_table_row(line: str) -> bool:
    return line.lstrip().startswith("|")


def _cell_count(line: str) -> int:
    stripped = line.strip().strip("|")
    if not stripped:
        return 0
    return len(re.split(r"(?<!\\)\|", stripped))


_TABLE_DIVIDER_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*\|\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|\s*$"
)
_BROKEN_TABLE_SENTINELS: Final[tuple[str, ...]] = (
    "|| ---",
    "|##",
    "억원 |##",
    '{"kind":"table"',
    '"markdown":"',
)
SSE_RAW_SUFFIX: Final = ".sse"
