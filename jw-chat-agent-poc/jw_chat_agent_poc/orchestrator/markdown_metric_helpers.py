from __future__ import annotations

from typing import Any

from jw_chat_agent_poc.orchestrator.markdown_formatting import TABLE_LIMIT, eok_value, number_value, pct_value, rank_value, table


def metric_level_segments_md(data: dict[str, Any]) -> str:
    segments = data.get("level_segments")
    if not isinstance(segments, list):
        return ""
    rows = tuple(
        (rank_value(item.get("rank"), None), item.get("name"), pct_value(item.get("ms_recent_pct")), eok_value(None, item.get("value")))
        for item in segments[:TABLE_LIMIT]
        if isinstance(item, dict)
    )
    return table("### 분석 기준별 점유율", ("순위", "구분", "MS", "매출"), rows)


def metric_filter_rows(data: dict[str, Any]) -> tuple[tuple[str, Any], ...]:
    rows: list[tuple[str, Any]] = []
    applied = data.get("applied_filters")
    if isinstance(applied, dict):
        rows.extend(
            (_friendly_field(str(key)), _friendly_text(str(value)) if isinstance(value, str) else value)
            for key, value in applied.items()
        )
    unsupported = data.get("unsupported_filters")
    if isinstance(unsupported, list):
        rows.extend(_unsupported_filter_rows(unsupported))
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


def _unsupported_filter_rows(unsupported: list[Any]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for item in unsupported:
        if isinstance(item, dict):
            field = item.get("field") or "unsupported"
            value = item.get("value") or "-"
            reason = item.get("reason") or "지원하지 않는 지표 필터"
            rows.append((f"지원 안 됨: {_friendly_field(str(field))}", f"{value} ({_friendly_text(str(reason))})"))
    return rows


def _note_rows(label: str, values: list[Any]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for value in values:
        if isinstance(value, dict):
            rows.append((label, _note_value(value)))
        elif isinstance(value, str):
            rows.append((label, value))
    return rows


def _note_value(value: dict[str, Any]) -> str:
    requested = value.get("requested")
    interpreted = value.get("interpreted_as")
    basis = value.get("basis")
    if requested and interpreted:
        note = f"{requested} → {interpreted}"
        return f"{note} ({basis})" if basis else note
    raw = value.get("raw") or value.get("value") or "-"
    reason = value.get("reason") or value.get("note") or "-"
    return f"{raw} ({reason})"


def _data_basis_value(data_basis: dict[str, Any]) -> str:
    pairs = [
        f"{_friendly_field(str(key))}={_friendly_text(str(value))}"
        for key, value in data_basis.items()
        if value not in (None, "")
    ]
    return ", ".join(pairs) if pairs else "-"


def _friendly_field(field: str) -> str:
    labels = {
        "granularity": "세부 구분",
        "market_scope": "시장 범위",
        "view_type": "시장 기준",
        "relative_period": "상대 날짜",
        "relative_range": "상대 기간",
        "period_grain": "기간 단위",
        "latest_period": "최신 기간",
        "level": "분석 기준",
        "source": "데이터 소스",
    }
    return labels.get(field, field)


def _friendly_text(text: str) -> str:
    replacements = {
        "cache": "현재 데이터",
        "level": "분석 기준",
        "segment": "구간",
        "market_scope": "시장 범위",
        "view_type": "시장 기준",
        "cache period": "월 단위",
    }
    result = text
    for raw, friendly in replacements.items():
        result = result.replace(raw, friendly)
    return result
