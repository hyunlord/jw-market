from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from hashlib import sha256
import re
from typing import Any

from jw_chat_agent_poc.service.v4.lossless_contracts import (
    DeterministicRender,
    EvidenceSet,
    RenderNode,
)
from jw_chat_agent_poc.service.v4.lossless_spine import _omit_fully_unprovided_columns
from jw_chat_agent_poc.service.v4.source_labels import public_source_label


_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$")
_SEPARATOR_RE = re.compile(r"^:?-{3,}:?$")
_DATE_RE = re.compile(r"^(?:19|20)\d{2}(?:[-./]\d{1,2}){0,2}$")
_NUMBER_RE = re.compile(r"^[+-]?(?:\d{1,3}(?:,\d{3})*|\d+)(?:\.\d+)?(?:%|억원|원|명|건|일)?$")
_UNPROVIDED = "원천 미제공"


def build_grounded_tables(
    evidence_sets: Sequence[EvidenceSet],
    rendered: DeterministicRender,
) -> tuple[dict[str, Any], ...]:
    source_by_record = {
        record.evidence_id: evidence_set.source
        for evidence_set in evidence_sets
        for record in evidence_set.records
    }
    tables: list[dict[str, Any]] = []
    for node in rendered.nodes:
        visible_text, _ = _omit_fully_unprovided_columns(node.text.strip())
        visible_tables = _markdown_tables(visible_text)
        original_tables = _markdown_tables(node.text)
        for index, parsed in enumerate(visible_tables):
            record_ids = _bound_record_ids(node, len(parsed.rows))
            if not record_ids:
                continue
            columns = _columns(parsed.headers, parsed.separators, parsed.rows)
            keys = tuple(column["key"] for column in columns)
            sources = tuple(
                dict.fromkeys(
                    source_by_record[record_id]
                    for record_id in record_ids
                    if record_id in source_by_record
                )
            )
            if not sources:
                continue
            omitted = (
                _omitted_columns(original_tables[index])
                if index < len(original_tables)
                else ()
            )
            identity = "\x1f".join(
                (node.block_id, str(index), *parsed.headers, *record_ids)
            )
            tables.append(
                {
                    "table_id": "v4-" + sha256(identity.encode("utf-8")).hexdigest()[:16],
                    "title": parsed.title or node.block_id,
                    "source_label": " · ".join(public_source_label(source) for source in sources),
                    "columns": columns,
                    "rows": [
                        {
                            "cells": dict(zip(keys, row, strict=True)),
                            "record_id": record_id,
                        }
                        for row, record_id in zip(parsed.rows, record_ids, strict=True)
                    ],
                    "row_count": len(parsed.rows),
                    "omitted_columns": list(omitted),
                }
            )
    return tuple(tables)


def filter_charts_bound_to_tables(
    charts: Sequence[Mapping[str, Any]],
    tables: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    values_by_record: dict[str, set[str]] = {}
    for table in tables:
        rows = table.get("rows")
        if not isinstance(rows, Sequence) or isinstance(rows, str | bytes):
            continue
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            record_id = row.get("record_id")
            cells = row.get("cells")
            if not isinstance(record_id, str) or not isinstance(cells, Mapping):
                continue
            values_by_record.setdefault(record_id, set()).update(
                _comparable_values(cells.values())
            )

    bound: list[dict[str, Any]] = []
    for chart in charts:
        series = chart.get("series")
        if not isinstance(series, Sequence) or isinstance(series, str | bytes):
            continue
        points = 0
        valid = True
        for item in series:
            if not isinstance(item, Mapping):
                valid = False
                break
            values = item.get("values")
            record_ids = item.get("record_ids")
            if (
                not isinstance(values, Sequence)
                or isinstance(values, str | bytes)
                or not isinstance(record_ids, Sequence)
                or isinstance(record_ids, str | bytes)
                or len(values) != len(record_ids)
            ):
                valid = False
                break
            points += len(values)
            for value, record_id in zip(values, record_ids, strict=True):
                if (
                    not isinstance(record_id, str)
                    or _comparable_value(value)
                    not in values_by_record.get(record_id, set())
                ):
                    valid = False
                    break
            if not valid:
                break
        if valid and points >= 2:
            bound.append(dict(chart))
    return tuple(bound)


class _ParsedTable:
    __slots__ = ("headers", "rows", "separators", "title")

    def __init__(
        self,
        *,
        title: str,
        headers: tuple[str, ...],
        separators: tuple[str, ...],
        rows: tuple[tuple[str, ...], ...],
    ) -> None:
        self.title = title
        self.headers = headers
        self.separators = separators
        self.rows = rows


def _markdown_tables(text: str) -> tuple[_ParsedTable, ...]:
    lines = text.splitlines()
    tables: list[_ParsedTable] = []
    heading = ""
    index = 0
    while index < len(lines):
        heading_match = _HEADING_RE.fullmatch(lines[index].strip())
        if heading_match:
            heading = heading_match.group(1).strip()
            index += 1
            continue
        headers = _split_row(lines[index])
        separators = _split_row(lines[index + 1]) if index + 1 < len(lines) else None
        if (
            headers is None
            or separators is None
            or len(headers) != len(separators)
            or not all(_SEPARATOR_RE.fullmatch(cell) for cell in separators)
        ):
            index += 1
            continue
        rows: list[tuple[str, ...]] = []
        cursor = index + 2
        while cursor < len(lines):
            row = _split_row(lines[cursor])
            if row is None or len(row) != len(headers):
                break
            rows.append(row)
            cursor += 1
        if rows and not _placeholder_only(rows):
            tables.append(
                _ParsedTable(
                    title=heading,
                    headers=headers,
                    separators=separators,
                    rows=tuple(rows),
                )
            )
        index = cursor
    return tuple(tables)


def _split_row(line: str) -> tuple[str, ...] | None:
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return None
    return tuple(cell.strip() for cell in re.split(r"(?<!\\)\|", stripped[1:-1]))


def _placeholder_only(rows: Sequence[Sequence[str]]) -> bool:
    return len(rows) == 1 and rows[0] and rows[0][0] == "조회 결과 없음"


def _bound_record_ids(node: RenderNode, row_count: int) -> tuple[str, ...]:
    if row_count <= 0 or not node.record_ids:
        return ()
    if len(node.record_ids) == row_count:
        return node.record_ids
    if len(node.record_ids) == 1:
        return node.record_ids * row_count
    return ()


def _omitted_columns(table: _ParsedTable) -> tuple[str, ...]:
    return tuple(
        header
        for index, header in enumerate(table.headers)
        if table.rows and all(row[index].strip() == _UNPROVIDED for row in table.rows)
    )


def _columns(
    headers: Sequence[str],
    separators: Sequence[str],
    rows: Sequence[Sequence[str]],
) -> list[dict[str, Any]]:
    columns: list[dict[str, Any]] = []
    for index, (label, separator) in enumerate(zip(headers, separators, strict=True), start=1):
        values = tuple(row[index - 1] for row in rows)
        column_type = _column_type(values)
        columns.append(
            {
                "key": f"column_{index}",
                "label": label,
                "type": column_type,
                "unit": _unit(label, values),
                "align": (
                    "center"
                    if separator.startswith(":") and separator.endswith(":")
                    else "right"
                    if separator.endswith(":") or column_type == "number"
                    else "left"
                ),
            }
        )
    return columns


def _column_type(values: Sequence[str]) -> str:
    cleaned = tuple(_plain_cell(value) for value in values if _plain_cell(value))
    if cleaned and all(_DATE_RE.fullmatch(value) for value in cleaned):
        return "date"
    if cleaned and all(_NUMBER_RE.fullmatch(value.replace(" ", "")) for value in cleaned):
        return "number"
    return "string"


def _unit(label: str, values: Sequence[str]) -> str | None:
    combined = " ".join((label, *values))
    for unit in ("억원", "%", "원", "명", "건", "일"):
        if unit in combined:
            return unit
    return None


def _plain_cell(value: str) -> str:
    link = re.fullmatch(r"\[([^\]]+)\]\([^)]+\)", value.strip())
    return (link.group(1) if link else value).strip()


def _comparable_values(values: Iterable[Any]) -> set[str]:
    comparable: set[str] = set()
    for value in values:
        plain = _plain_cell(str(value))
        comparable.add(plain)
        compact = plain.replace(",", "").replace(" ", "")
        number = re.fullmatch(r"([+-]?\d+(?:\.\d+)?)(?:%|억원|원|명|건|일)?", compact)
        if number:
            comparable.add(_normalize_number(number.group(1)))
    return comparable


def _comparable_value(value: Any) -> str:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return _normalize_number(str(value))
    return str(value).strip()


def _normalize_number(value: str) -> str:
    number = float(value)
    return str(int(number)) if number.is_integer() else format(number, ".15g")


__all__ = ["build_grounded_tables", "filter_charts_bound_to_tables"]
