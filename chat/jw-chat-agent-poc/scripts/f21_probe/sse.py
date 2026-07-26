from __future__ import annotations

import json
from typing import TypeAlias


JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]


def parse_sse(raw: str) -> list[JsonObject]:
    events: list[JsonObject] = []
    for block in raw.replace("\r\n", "\n").split("\n\n"):
        if not block.strip():
            continue
        name = "message"
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line.removeprefix("event:").strip()
            elif line.startswith("data:"):
                data_lines.append(line.removeprefix("data:").lstrip())
        raw_data = "\n".join(data_lines)
        try:
            data: JsonValue = json.loads(raw_data)
        except json.JSONDecodeError:
            data = raw_data
        events.append({"event": name, "data": data})
    return events


def latest_object(events: list[JsonObject], name: str) -> JsonObject:
    for event in reversed(events):
        if event.get("event") == name and isinstance(event.get("data"), dict):
            return event["data"]
    return {}


def render_answer(events: list[JsonObject]) -> str:
    chunks: list[str] = []
    for event in events:
        data = event.get("data")
        if event.get("event") == "delta" and isinstance(data, str):
            chunks.append(data)
        elif event.get("event") == "markdown_block" and isinstance(data, dict):
            markdown = data.get("markdown")
            if isinstance(markdown, str):
                chunks.append(markdown)
    return "".join(chunks).strip()


def event_names(events: list[JsonObject]) -> list[str]:
    return [
        name
        for event in events
        if isinstance((name := event.get("event")), str)
    ]


def extract_tools(qa_trace: JsonObject, trace: JsonObject) -> list[str]:
    tools = qa_trace.get("tools")
    if isinstance(tools, list):
        names = [
            name
            for item in tools
            if isinstance(item, dict)
            and isinstance((name := item.get("name")), str)
        ]
        if names:
            return names
    raw_tools = trace.get("tools_called") or trace.get("tools") or []
    return [str(item) for item in raw_tools] if isinstance(raw_tools, list) else []


def object_value(parent: JsonObject, key: str) -> JsonObject:
    value = parent.get(key)
    return value if isinstance(value, dict) else {}
