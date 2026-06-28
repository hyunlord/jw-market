from __future__ import annotations

from typing import Any

from jw_chat_agent_poc.orchestrator.markdown_formatting import number_value, table
from jw_chat_agent_poc.orchestrator.markdown_formatting import items as render_items


def news_md(data: dict[str, Any]) -> str:
    rows = tuple(
        (
            item.get("date"),
            _news_title(item),
            item.get("source"),
            number_value(item.get("impact_score")),
            item.get("summary"),
            item.get("match_excerpt"),
        )
        for item in render_items(data)
    )
    blocks = [table("### 관련 뉴스", ("날짜", "제목", "출처", "impact", "요약", "매칭 발췌"), rows)]
    source_rows = (("최신일", data.get("latest_event_date") or "-"), ("선별", data.get("selection") or "on_list=true 우선"))
    blocks.append(table("### 뉴스 기준", ("항목", "값"), source_rows))
    filter_rows = _news_filter_rows(data)
    if filter_rows:
        blocks.append(table("### 뉴스 필터", ("구분", "값"), filter_rows))
    if data.get("status") == "no_data":
        blocks.append(table("### 상태", ("항목", "값"), (("상태", data.get("message") or "관련 뉴스 없음"),)))
    return "\n\n".join(blocks)


def _news_filter_rows(data: dict[str, Any]) -> tuple[tuple[str, Any], ...]:
    rows: list[tuple[str, Any]] = []
    applied = data.get("applied_filters")
    if isinstance(applied, dict):
        rows.extend((str(key), value) for key, value in applied.items())
    unsupported = data.get("unsupported_filters")
    if isinstance(unsupported, list):
        for item in unsupported:
            if isinstance(item, dict):
                field = item.get("field") or "unsupported"
                value = item.get("value") or "-"
                reason = item.get("reason") or "지원하지 않는 뉴스 필터"
                rows.append((f"지원 안 됨: {field}", f"{value} ({reason})"))
    interpretation = data.get("interpretation_notes")
    if isinstance(interpretation, list):
        rows.extend(_note_rows("해석 가정", interpretation))
    unparsed = data.get("unparsed_constraints")
    if isinstance(unparsed, list):
        rows.extend(_note_rows("파싱 못 함", unparsed))
    data_basis = data.get("data_basis")
    if isinstance(data_basis, dict):
        rows.append(("데이터 기준", _data_basis_value(data_basis)))
    return tuple(rows)


def _note_rows(label: str, values: list[Any]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for value in values:
        if isinstance(value, dict):
            raw = value.get("raw") or value.get("value") or "-"
            reason = value.get("reason") or value.get("note") or "-"
            rows.append((label, f"{raw} ({reason})"))
        elif isinstance(value, str):
            rows.append((label, value))
    return rows


def _data_basis_value(data_basis: dict[str, Any]) -> str:
    pairs = [f"{key}={value}" for key, value in data_basis.items() if key != "source" and value not in (None, "")]
    return ", ".join(pairs) if pairs else "-"


def _news_title(item: dict[str, Any]) -> str:
    title = str(item.get("title") or "-")
    url = item.get("url")
    if isinstance(url, str) and url:
        safe_title = title.replace("]", "\\]")
        return f"[{safe_title}]({url})"
    return title
