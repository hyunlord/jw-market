"""Identify portal workbook sources from canonical header contracts only."""
from __future__ import annotations

import posixpath
from pathlib import Path
import re
from typing import TypeAlias
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

from pipeline.etl.io.source_headers import normalize_source_header


class SourceValidationError(ValueError):
    """Workbook headers do not identify exactly one supported source."""


_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_DOC_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CELL_REF = re.compile(r"([A-Z]+)")
_SharedRef: TypeAlias = tuple[str, int]


def _column_index(reference: str) -> int:
    match = _CELL_REF.match(reference.upper())
    if match is None:
        raise SourceValidationError(f"invalid XLSX cell reference: {reference!r}")
    result = 0
    for character in match.group(1):
        result = result * 26 + ord(character) - ord("A") + 1
    return result - 1


def _sheet_paths(archive: ZipFile) -> tuple[str, ...]:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {
        relation.attrib["Id"]: relation.attrib["Target"]
        for relation in relationships.findall(f"{{{_PKG_REL_NS}}}Relationship")
    }
    relationship_id = f"{{{_DOC_REL_NS}}}id"
    paths: list[str] = []
    for sheet in workbook.findall(f".//{{{_MAIN_NS}}}sheet"):
        target = targets[sheet.attrib[relationship_id]]
        if target.startswith("/"):
            paths.append(target.lstrip("/"))
        else:
            paths.append(posixpath.normpath(posixpath.join("xl", target)))
    return tuple(paths)


def _raw_header_rows(
    archive: ZipFile, sheet_path: str
) -> tuple[dict[int, dict[int, str | _SharedRef]], set[int]]:
    rows: dict[int, dict[int, str | _SharedRef]] = {}
    shared_indexes: set[int] = set()
    with archive.open(sheet_path) as handle:
        for _event, element in ET.iterparse(handle, events=("end",)):
            if element.tag != f"{{{_MAIN_NS}}}row":
                continue
            row_number = int(element.attrib.get("r", "0"))
            if row_number in {1, 2, 7}:
                values: dict[int, str | _SharedRef] = {}
                for cell in element.findall(f"{{{_MAIN_NS}}}c"):
                    reference = cell.attrib.get("r", "")
                    column = _column_index(reference)
                    cell_type = cell.attrib.get("t")
                    if cell_type == "inlineStr":
                        text = "".join(
                            node.text or "" for node in cell.iter(f"{{{_MAIN_NS}}}t")
                        )
                        values[column] = text
                        continue
                    value = cell.find(f"{{{_MAIN_NS}}}v")
                    text = "" if value is None else value.text or ""
                    if cell_type == "s" and text:
                        index = int(text)
                        values[column] = ("shared", index)
                        shared_indexes.add(index)
                    else:
                        values[column] = text
                rows[row_number] = values
            element.clear()
            if row_number >= 7:
                break
    return rows, shared_indexes


def _shared_strings(archive: ZipFile, wanted: set[int]) -> dict[int, str]:
    if not wanted or "xl/sharedStrings.xml" not in archive.namelist():
        return {}
    resolved: dict[int, str] = {}
    index = 0
    with archive.open("xl/sharedStrings.xml") as handle:
        for _event, element in ET.iterparse(handle, events=("end",)):
            if element.tag != f"{{{_MAIN_NS}}}si":
                continue
            if index in wanted:
                resolved[index] = "".join(
                    node.text or "" for node in element.iter(f"{{{_MAIN_NS}}}t")
                )
                if len(resolved) == len(wanted):
                    break
            index += 1
            element.clear()
    return resolved


def _header_rows(path: Path) -> tuple[tuple[tuple[object, ...], ...], ...]:
    with ZipFile(path) as archive:
        raw_sheets = []
        wanted: set[int] = set()
        for sheet_path in _sheet_paths(archive):
            rows, indexes = _raw_header_rows(archive, sheet_path)
            raw_sheets.append(rows)
            wanted.update(indexes)
        shared = _shared_strings(archive, wanted)

    result = []
    for rows in raw_sheets:
        resolved_rows = []
        for row_number in (1, 2, 7):
            values = rows.get(row_number, {})
            width = max(values, default=-1) + 1
            resolved_rows.append(
                tuple(
                    shared.get(value[1], "")
                    if isinstance(value, tuple) and value[0] == "shared"
                    else value
                    for value in (values.get(column, "") for column in range(width))
                )
            )
        result.append(tuple(resolved_rows))
    return tuple(result)


def _contains(headers: tuple[object, ...], required: tuple[str, ...]) -> bool:
    present = {normalize_source_header(value) for value in headers if value is not None}
    return all(normalize_source_header(value) in present for value in required)


def detect_workbook_source(path: Path) -> str:
    """Return one source category without consulting path, filename, or sheet name."""
    from pipeline.etl.io.iqvia_loader import (
        NSA_PARQUET_METRICS,
        NSA_PERIOD_HEADER,
        NSA_REQUIRED_STATIC_HEADERS,
    )
    from pipeline.etl.io.ubist_loader import classify_sheet
    from pipeline.scripts.etl.brand_activity.csd_core import EXPECTED_HEADERS
    from pipeline.scripts.etl.brand_activity.ingest_keyword import KEYWORD_HEADERS

    matches: set[str] = set()
    try:
        sheets = _header_rows(path)
    except (BadZipFile, KeyError, OSError, ET.ParseError) as exc:
        raise SourceValidationError(f"invalid XLSX structure: {type(exc).__name__}") from exc
    for first, second, seventh in sheets:
        if _contains(first, KEYWORD_HEADERS):
            matches.add("iqvia_csd_keyword")
        normalized_first = {
            normalize_source_header(value) for value in first if value is not None
        }
        if _contains(first, NSA_REQUIRED_STATIC_HEADERS) and (
            normalize_source_header(NSA_PERIOD_HEADER) in normalized_first
            or any(
                normalize_source_header(metric) in normalized_first
                for metric in NSA_PARQUET_METRICS
            )
        ):
            matches.add("iqvia_nsa")
        if _contains(seventh, EXPECTED_HEADERS):
            matches.add("iqvia_csd_channel")
        try:
            classify_sheet("", first, second)
        except (RuntimeError, ValueError):
            pass
        else:
            matches.add("ubist")

    if len(matches) != 1:
        detail = "unrecognized" if not matches else f"ambiguous: {sorted(matches)}"
        raise SourceValidationError(f"workbook source {detail}")
    return next(iter(matches))
