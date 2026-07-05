"""XLSX-specific text extraction for wf301 uploaded market tables."""

from __future__ import annotations

from pathlib import Path
from typing import Final
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile


SHEET_NS: Final = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL_NS: Final = "{http://schemas.openxmlformats.org/package/2006/relationships}"
OFFICE_REL_NS: Final = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
DEFAULT_CHUNK_CHAR_LIMIT: Final = 1800


class XlsxPreprocessError(RuntimeError):
    """Raised when an XLSX workbook cannot be converted into searchable chunks."""


def extract_xlsx_chunks(path: Path, *, chunk_char_limit: int = DEFAULT_CHUNK_CHAR_LIMIT) -> list[str]:
    """Return header-value-preserving chunks from an XLSX workbook."""
    if chunk_char_limit < 80:
        raise XlsxPreprocessError("chunk_char_limit must be at least 80")
    try:
        with ZipFile(path) as archive:
            shared_strings = _shared_strings(archive)
            sheet_paths = _sheet_paths(archive)
            chunks: list[str] = []
            for sheet_name, sheet_path in sheet_paths:
                rows = _sheet_rows(archive, sheet_path, shared_strings)
                chunks.extend(_chunks_for_sheet(sheet_name, rows, chunk_char_limit))
    except (BadZipFile, ElementTree.ParseError, KeyError, OSError) as exc:
        raise XlsxPreprocessError(f"xlsx preprocessing failed: {exc}") from exc
    if not chunks:
        raise XlsxPreprocessError("xlsx preprocessing produced no chunks")
    return chunks


def _shared_strings(archive: ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    values: list[str] = []
    for item in root.findall(f"{SHEET_NS}si"):
        parts = [node.text or "" for node in item.iter(f"{SHEET_NS}t")]
        values.append("".join(parts).strip())
    return values


def _sheet_paths(archive: ZipFile) -> list[tuple[str, str]]:
    workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    rels = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    rel_targets = {
        str(rel.attrib.get("Id") or ""): _normalize_sheet_target(str(rel.attrib.get("Target") or ""))
        for rel in rels.findall(f"{REL_NS}Relationship")
    }
    sheets: list[tuple[str, str]] = []
    for sheet in workbook.findall(f".//{SHEET_NS}sheet"):
        rel_id = str(sheet.attrib.get(f"{OFFICE_REL_NS}id") or "")
        target = rel_targets.get(rel_id)
        if target:
            sheets.append((str(sheet.attrib.get("name") or rel_id), target))
    return sheets


def _normalize_sheet_target(target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    if target.startswith("xl/"):
        return target
    return f"xl/{target}"


def _sheet_rows(archive: ZipFile, sheet_path: str, shared_strings: list[str]) -> list[list[str]]:
    root = ElementTree.fromstring(archive.read(sheet_path))
    rows: list[list[str]] = []
    for row in root.findall(f".//{SHEET_NS}row"):
        values: list[str] = []
        for cell in row.findall(f"{SHEET_NS}c"):
            values.append(_cell_value(cell, shared_strings))
        if any(value.strip() for value in values):
            rows.append(values)
    return rows


def _cell_value(cell: ElementTree.Element, shared_strings: list[str]) -> str:
    cell_type = cell.attrib.get("t")
    match cell_type:
        case "inlineStr":
            text = "".join(node.text or "" for node in cell.iter(f"{SHEET_NS}t"))
            return _clean_value(text)
        case "s":
            raw_index = cell.findtext(f"{SHEET_NS}v") or ""
            index = int(raw_index) if raw_index.isdigit() else -1
            if 0 <= index < len(shared_strings):
                return _clean_value(shared_strings[index])
            return ""
        case "b":
            value = cell.findtext(f"{SHEET_NS}v") or ""
            return "TRUE" if value == "1" else "FALSE"
        case "str" | "e" | None:
            return _clean_value(cell.findtext(f"{SHEET_NS}v") or "")
        case _:
            return _clean_value(cell.findtext(f"{SHEET_NS}v") or "")


def _chunks_for_sheet(sheet_name: str, rows: list[list[str]], chunk_char_limit: int) -> list[str]:
    header = _first_header(rows)
    if header is None:
        return []
    header_index, headers = header
    chunks: list[str] = []
    current = ""
    for row_number, row in enumerate(rows[header_index + 1 :], start=header_index + 2):
        line = _row_line(sheet_name, row_number, headers, row)
        if not line:
            continue
        if len(line) > chunk_char_limit:
            chunks.extend(_split_long_line(line, chunk_char_limit))
            continue
        if current and len(current) + 1 + len(line) > chunk_char_limit:
            chunks.append(current)
            current = line
        else:
            current = line if not current else f"{current}\n{line}"
    if current:
        chunks.append(current)
    return chunks


def _first_header(rows: list[list[str]]) -> tuple[int, list[str]] | None:
    candidates: list[tuple[int, int, list[str]]] = []
    for index, row in enumerate(rows[:10]):
        cleaned = [_clean_header(value) for value in row]
        score = sum(bool(value) for value in cleaned)
        if score >= 2:
            candidates.append((score, index, cleaned))
    if candidates:
        _score, index, cleaned = max(candidates, key=lambda item: (item[0], -item[1]))
        headers = _merge_group_headers(rows[:index], cleaned)
        return index, _dedupe_headers(headers)
    return None


def _merge_group_headers(previous_rows: list[list[str]], header_row: list[str]) -> list[str]:
    headers: list[str] = []
    for column_index, header in enumerate(header_row):
        groups: list[str] = []
        for previous in previous_rows:
            value = _clean_header(previous[column_index]) if column_index < len(previous) else ""
            if value and value not in groups:
                groups.append(value)
        if header:
            groups.append(header)
        headers.append(" ".join(groups))
    return headers


def _dedupe_headers(headers: list[str]) -> list[str]:
    result: list[str] = []
    seen: dict[str, int] = {}
    for index, header in enumerate(headers, start=1):
        label = header or f"컬럼{index}"
        count = seen.get(label, 0) + 1
        seen[label] = count
        result.append(label if count == 1 else f"{label}_{count}")
    return result


def _row_line(sheet_name: str, row_number: int, headers: list[str], row: list[str]) -> str:
    pairs: list[str] = []
    for index, header in enumerate(headers):
        value = row[index].strip() if index < len(row) else ""
        if value:
            pairs.append(f"{header}: {value}")
    if not pairs:
        return ""
    return f"시트: {sheet_name} | 행: {row_number} | " + " | ".join(pairs)


def _split_long_line(line: str, chunk_char_limit: int) -> list[str]:
    parts = line.split(" | ")
    chunks: list[str] = []
    current = ""
    for part in parts:
        candidate = part if not current else f"{current} | {part}"
        if len(candidate) <= chunk_char_limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = part[:chunk_char_limit]
    if current:
        chunks.append(current)
    return chunks


def _clean_header(value: str) -> str:
    cleaned = _clean_value(value)
    if cleaned.lower() in {"none", "null", "nan", "unnamed"}:
        return ""
    return cleaned


def _clean_value(value: str) -> str:
    return " ".join(value.replace("\x00", " ").split())
