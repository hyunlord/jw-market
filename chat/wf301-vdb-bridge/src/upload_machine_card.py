"""Fast, deterministic metadata for an upload acknowledgement card."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile, ZipFile

import fitz

from .xlsx_preprocessor import _sheet_paths


_DIMENSION_RE = re.compile(rb"<dimension\s+[^>]*ref=\"(?:[^:\"]+:)?([A-Z]+)([0-9]+)\"")
_PPTX_SLIDE_RE = re.compile(r"^ppt/slides/slide[0-9]+\.xml$")


@dataclass(frozen=True, slots=True)
class ObservedSheet:
    name: str
    row_count: int | None = None
    column_count: int | None = None


@dataclass(frozen=True, slots=True)
class UploadMachineCard:
    file_type: str
    size_bytes: int
    sheet_count: int | None = None
    sheets: tuple[ObservedSheet, ...] = ()
    page_count: int | None = None
    slide_count: int | None = None


def inspect_upload_machine_card(path: Path, file_name: str) -> UploadMachineCard:
    """Inspect package headers only; corrupt files get a safe minimal card."""

    suffix = Path(file_name).suffix.lower().lstrip(".") or "file"
    try:
        size_bytes = path.stat().st_size
    except OSError:
        size_bytes = 0

    if suffix in {"xlsx", "xlsm"}:
        sheets = _inspect_xlsx_sheets(path)
        return UploadMachineCard(
            file_type=suffix,
            size_bytes=size_bytes,
            sheet_count=len(sheets) if sheets is not None else None,
            sheets=sheets or (),
        )
    if suffix == "pdf":
        return UploadMachineCard(
            file_type=suffix,
            size_bytes=size_bytes,
            page_count=_pdf_page_count(path),
        )
    if suffix == "pptx":
        return UploadMachineCard(
            file_type=suffix,
            size_bytes=size_bytes,
            slide_count=_pptx_slide_count(path),
        )
    return UploadMachineCard(file_type=suffix, size_bytes=size_bytes)


def _inspect_xlsx_sheets(path: Path) -> tuple[ObservedSheet, ...] | None:
    try:
        with ZipFile(path) as archive:
            sheets: list[ObservedSheet] = []
            for name, member in _sheet_paths(archive):
                row_count, column_count = _worksheet_dimension(archive, member)
                sheets.append(
                    ObservedSheet(
                        name=name,
                        row_count=row_count,
                        column_count=column_count,
                    )
                )
            return tuple(sheets)
    except (BadZipFile, KeyError, OSError, ParseError, ValueError):
        return None


def _worksheet_dimension(archive: ZipFile, member: str) -> tuple[int | None, int | None]:
    try:
        with archive.open(member) as source:
            header = source.read(65_536)
    except (KeyError, OSError):
        return None, None
    match = _DIMENSION_RE.search(header)
    if match is None:
        return None, None
    return int(match.group(2)), _column_number(match.group(1).decode("ascii"))


def _column_number(letters: str) -> int:
    value = 0
    for letter in letters:
        value = value * 26 + ord(letter) - ord("A") + 1
    return value


def _pdf_page_count(path: Path) -> int | None:
    try:
        with fitz.open(path) as document:
            return len(document)
    except (OSError, RuntimeError, ValueError):
        return None


def _pptx_slide_count(path: Path) -> int | None:
    try:
        with ZipFile(path) as archive:
            return sum(bool(_PPTX_SLIDE_RE.fullmatch(name)) for name in archive.namelist())
    except (BadZipFile, OSError):
        return None
