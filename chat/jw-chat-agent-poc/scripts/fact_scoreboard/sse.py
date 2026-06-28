from __future__ import annotations

import json
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
    delta_count: int
    done_count: int
    error_count: int
    answer_chars: int


def parse_sse_file(path: Path) -> SseCapture:
    """Parse a raw Server-Sent Events file emitted by /chat/stream."""

    events = _events(path.read_text(encoding="utf-8"))
    answer_parts: list[str] = []
    charts: list[dict[str, object]] = []
    timing: dict[str, object] = {}
    sources = ""
    delta_count = 0
    done_count = 0
    error_count = 0
    for event_name, data in events:
        match event_name:
            case "delta":
                delta_count += 1
                answer_parts.append(data)
            case "sources":
                sources = data
            case "charts":
                charts.extend(_chart_items(data))
            case "timing":
                timing = _timing_item(data)
            case "done":
                done_count += 1
            case "error":
                error_count += 1
            case "conversation":
                continue
            case _:
                continue
    answer = "".join(answer_parts)
    return SseCapture(
        answer_markdown=answer,
        sources=sources,
        charts=tuple(charts),
        timing=timing,
        delta_count=delta_count,
        done_count=done_count,
        error_count=error_count,
        answer_chars=len(answer),
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
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


SSE_RAW_SUFFIX: Final = ".sse"
