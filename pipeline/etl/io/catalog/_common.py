from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

STANDARD_PREFIX = "drug_extra_json."


def utc_now_text() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")


def to_jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def dumps_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=to_jsonable, sort_keys=True)


def normalize_header(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def cell_text(value: object) -> str | None:
    return normalize_header(value)


def is_empty_row(values: list[Any] | tuple[Any, ...]) -> bool:
    return all(value is None or str(value).strip() == "" for value in values)


def make_header_keys(headers: list[Any] | tuple[Any, ...]) -> list[str]:
    seen: dict[str, int] = {}
    keys: list[str] = []
    for index, header in enumerate(headers, start=1):
        normalized = normalize_header(header)
        base = normalized if normalized is not None else f"__blank_col_{index}"
        count = seen.get(base, 0) + 1
        seen[base] = count
        keys.append(base if count == 1 else f"{base}__{count}")
    return keys


def build_raw_row_payload(
    headers: list[Any] | tuple[Any, ...],
    values: list[Any] | tuple[Any, ...],
    source_row_id: int,
) -> dict[str, Any]:
    width = max(len(headers), len(values))
    padded_headers = list(headers) + [None] * (width - len(headers))
    padded_values = list(values) + [None] * (width - len(values))
    keys = make_header_keys(padded_headers)
    cells = []
    values_by_header: dict[str, Any] = {}
    for index, (header, key, value) in enumerate(zip(padded_headers, keys, padded_values), start=1):
        json_value = to_jsonable(value)
        cell = {
            "column_index": index,
            "header": normalize_header(header),
            "header_key": key,
            "value": json_value,
        }
        cells.append(cell)
        values_by_header[key] = json_value
    return {
        "source_row_id": source_row_id,
        "cells": cells,
        "values_by_header": values_by_header,
    }


def _header_lookup(headers: list[Any] | tuple[Any, ...], values: list[Any] | tuple[Any, ...]) -> dict[str, Any]:
    lookup: dict[str, Any] = {}
    for header, value in zip(headers, values):
        normalized = normalize_header(header)
        if normalized is not None and normalized not in lookup:
            lookup[normalized] = value
    return lookup


def _lookup_source_value(lookup: dict[str, Any], source_column: str | None) -> Any:
    if source_column is None:
        return None
    if source_column in lookup:
        return lookup[source_column]
    for header, value in lookup.items():
        if header.startswith(source_column):
            return value
    return None


def _lookup_position_value(values: list[Any] | tuple[Any, ...], position: Any) -> Any:
    if position is None:
        return None
    try:
        column_index = int(position)
    except (TypeError, ValueError):
        raise ValueError(f"invalid catalog position: {position!r}") from None
    if column_index < 0:
        raise ValueError(f"catalog position must be >= 0: {position!r}")
    if column_index >= len(values):
        return None
    return values[column_index]


def _position_value(values: list[Any] | tuple[Any, ...], column_index: int) -> Any:
    if column_index <= 0 or column_index > len(values):
        return None
    return values[column_index - 1]


def _lookup_key(*values: Any) -> tuple[str, ...] | None:
    key: list[str] = []
    for value in values:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        key.append(text)
    return tuple(key)


def _single_lookup_key(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def explicit_lookup_join(data_rows: list[tuple[int, tuple[Any, ...]]]) -> dict[int, dict[str, Any]]:
    lookup1: dict[tuple[str, str], dict[str, Any]] = {}
    for _, values in data_rows:
        key = _lookup_key(_position_value(values, 17), _position_value(values, 18))
        if key and key not in lookup1:
            lookup1[key] = {
                "molecule": _position_value(values, 19),
                "molecule_disease_definition": _position_value(values, 20),
                "composition_type": _position_value(values, 21),
                "class": _position_value(values, 22),
            }

    lookup2: dict[str, Any] = {}
    for _, values in data_rows:
        key = _single_lookup_key(_position_value(values, 25))
        if key and key not in lookup2:
            lookup2[key] = _position_value(values, 26)

    overrides: dict[int, dict[str, Any]] = {}
    for source_row_id, values in data_rows:
        left_key = _lookup_key(_position_value(values, 2), _position_value(values, 3))
        if not left_key or left_key not in lookup1:
            continue
        row_override = dict(lookup1[left_key])
        molecule_key = _single_lookup_key(row_override.get("molecule"))
        row_override["class_2"] = lookup2.get(molecule_key) if molecule_key else None
        overrides[source_row_id] = row_override
    return overrides


def _extra_key(metadata_key: str) -> str:
    return metadata_key[len(STANDARD_PREFIX) :]


def apply_column_mapping(
    headers: list[Any] | tuple[Any, ...],
    values: list[Any] | tuple[Any, ...],
    metadata: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    lookup = _header_lookup(headers, values)
    standard: dict[str, Any] = {}
    extras: dict[str, Any] = {}
    for target_column, spec in metadata.items():
        source_column = normalize_header(spec.get("source_column"))
        if "position" in spec and spec.get("position") is not None:
            value = _lookup_position_value(values, spec.get("position"))
        else:
            value = _lookup_source_value(lookup, source_column)
        if target_column.startswith(STANDARD_PREFIX):
            extras[_extra_key(target_column)] = to_jsonable(value)
        else:
            standard[target_column] = value
    return standard, extras


def load_column_metadata_catalog(path: Path, expected_market_ids: set[str]) -> dict[str, dict[str, dict[str, Any]]]:
    import re

    text = path.read_text(encoding="utf-8")
    catalog: dict[str, dict[str, dict[str, Any]]] = {}
    current_market: str | None = None
    lines = iter(text.splitlines())
    for line in lines:
        heading = re.match(r"^###\s+(strategy_\d{3})\s+—", line)
        if heading:
            current_market = heading.group(1)
            continue
        if current_market and line.strip() == "```json":
            block: list[str] = []
            for json_line in lines:
                if json_line.strip() == "```":
                    break
                block.append(json_line)
            catalog[current_market] = json.loads("\n".join(block))
            current_market = None
    missing = sorted(expected_market_ids - set(catalog))
    if missing:
        raise ValueError(f"column metadata catalog missing markets: {missing}")
    return catalog


def write_records_parquet(
    records: list[dict[str, Any]],
    columns: tuple[str, ...],
    output_file: Path,
    *,
    compression_level: int | None = None,
    stringify: bool = False,
) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    schema = pa.schema([pa.field(column, pa.string()) for column in columns])
    rows = records
    if stringify:
        rows = [
            {column: None if record[column] is None else str(record[column]) for column in columns}
            for record in records
        ]
    table = pa.Table.from_pylist(rows, schema=schema)
    kwargs: dict[str, Any] = {"compression": "zstd"}
    if compression_level is not None:
        kwargs["compression_level"] = compression_level
    pq.write_table(table, output_file, **kwargs)
