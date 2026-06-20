"""Shared Keyword workbook parsing primitives for isolated stage loads."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
import hashlib
from pathlib import Path
import re
from typing import Final, Protocol, TypeAlias
import unicodedata

from openpyxl.worksheet._read_only import ReadOnlyWorksheet
from openpyxl.worksheet.worksheet import Worksheet


CellValue: TypeAlias = str | int | float | bool | datetime | date | None
JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
WorksheetLike: TypeAlias = Worksheet | ReadOnlyWorksheet

MONTHS: Final[dict[str, int]] = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
MESSAGE_MONTH_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|june|jul|july|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s*(\d{2}|\d{4})\b",
    re.IGNORECASE,
)


class KmParseError(ValueError):
    """Raised when a Keyword workbook value cannot be parsed safely."""


class ProductPeriodEvent(Protocol):
    """Protocol for Keyword rows that can be counted by product and source month."""

    source_file: str
    product_name: str
    period_ym: str


@dataclass(frozen=True, slots=True)
class KeywordEvent:
    """Event-level row from the `Keywords` sheet; duplicate rows are meaningful."""

    period_ym: str
    visit_location: str
    specialty: str
    representing_company: str
    product_name: str
    therapeutic_class: str
    keyword_text: str
    interest: str
    prescription_frequency: str
    prescription_evolution: str
    abstract_lit: str
    patient_lit: str
    promotional_lit: str
    samples_left: str
    other_materials_left: str
    what_other_materials: str
    other_comments: str
    source_file: str
    source_sheet: str
    source_row_no: int
    source_file_sha256: str

    def to_stage_row(self) -> dict[str, JsonValue]:
        """Serialize the raw DB stage row that keeps the source keyword text."""
        return asdict(self)

    def to_redacted_row(self) -> dict[str, JsonValue]:
        """Serialize an audit row without dumping raw keyword text."""
        row = asdict(self)
        keyword_text = self.keyword_text
        what_other_materials = self.what_other_materials
        other_comments = self.other_comments
        del row["keyword_text"]
        del row["what_other_materials"]
        del row["other_comments"]
        row["keyword_text_len"] = len(keyword_text)
        row["keyword_text_language"] = language_bucket(keyword_text)
        row["keyword_text_sha256"] = text_sha256(keyword_text)
        row["what_other_materials_len"] = len(what_other_materials)
        row["what_other_materials_language"] = language_bucket(what_other_materials)
        row["what_other_materials_sha256"] = text_sha256(what_other_materials)
        row["other_comments_len"] = len(other_comments)
        row["other_comments_language"] = language_bucket(other_comments)
        row["other_comments_sha256"] = text_sha256(other_comments)
        return row


def normalize_text(value: CellValue) -> str:
    """Normalize spreadsheet values to comparable text without inventing data."""
    if value is None:
        return ""
    return unicodedata.normalize("NFC", str(value)).strip()


def normalize_spaces(value: str) -> str:
    """Collapse repeated whitespace while preserving visible source text."""
    return re.sub(r"\s+", " ", value).strip()


def normalize_key(value: str) -> str:
    """Build a case-insensitive key for workbook product matching."""
    return normalize_spaces(value).upper()


def text_sha256(value: str) -> str:
    """Hash sensitive text so audit files can reconcile rows without raw text."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def source_sha256(path: Path) -> str:
    """Hash a source workbook for lineage and package manifest evidence."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_period_ym(value: CellValue) -> str:
    """Parse workbook month/date values into `YYYY-MM`."""
    if isinstance(value, datetime | date):
        return f"{value.year:04d}-{value.month:02d}"
    text = normalize_spaces(normalize_text(value))
    date_match = re.search(r"\b(20\d{2})[/-](\d{1,2})(?:[/-]\d{1,2})?", text)
    if date_match is not None:
        return f"{int(date_match.group(1)):04d}-{int(date_match.group(2)):02d}"
    month_match = MESSAGE_MONTH_PATTERN.search(text)
    if month_match is not None:
        month = MONTHS[month_match.group(1).lower()]
        year_raw = int(month_match.group(2))
        year = 2000 + year_raw if year_raw < 100 else year_raw
        return f"{year:04d}-{month:02d}"
    reverse_match = re.search(r"\b(\d{2}|\d{4})\s*([A-Za-z]+)\b", text)
    if reverse_match is not None:
        month = MONTHS.get(reverse_match.group(2).lower())
        if month is not None:
            year_raw = int(reverse_match.group(1))
            year = 2000 + year_raw if year_raw < 100 else year_raw
            return f"{year:04d}-{month:02d}"
    raise KmParseError(f"unparseable Keyword period: {value!r}")


def parse_nullable_int(value: CellValue) -> int | None:
    """Parse integer-like counts while preserving blank source cells as null."""
    text = normalize_text(value).replace(",", "")
    if text == "":
        return None
    number = float(text)
    if not number.is_integer():
        raise KmParseError(f"non-integer value: {value!r}")
    return int(number)


def parse_count_value(value: CellValue) -> int:
    """Parse a Message Count cell, treating blank cells as zero counts."""
    parsed = parse_nullable_int(value)
    if parsed is None:
        return 0
    return parsed


def source_period_from_name(path: Path) -> str:
    """Extract the source file month from the PL-confirmed workbook filename."""
    match = MESSAGE_MONTH_PATTERN.search(path.name)
    if match is None:
        raise KmParseError(f"source filename has no month: {path.name}")
    return parse_period_ym(match.group(0))


def language_bucket(text: str) -> str:
    """Classify text language coarsely without exposing the original wording."""
    normalized = normalize_spaces(text)
    if normalized == "":
        return "empty"
    has_hangul = re.search(r"[가-힣]", normalized) is not None
    has_latin = re.search(r"[A-Za-z]", normalized) is not None
    if has_hangul and has_latin:
        return "mixed_ko_en"
    if has_hangul:
        return "korean"
    if has_latin:
        return "english"
    return "other"


def read_header_row(sheet: WorksheetLike, max_col: int = 80) -> list[str]:
    """Read the first row as trimmed headers while keeping column positions."""
    row = next(sheet.iter_rows(min_row=1, max_row=1, max_col=max_col, values_only=True))
    return [normalize_spaces(normalize_text(value)) for value in row]


def row_is_empty(values: tuple[CellValue, ...]) -> bool:
    """Return whether a worksheet row has no visible source values."""
    return not any(normalize_text(value) for value in values)


def header_index(headers: list[str], required_headers: tuple[str, ...]) -> dict[str, int]:
    """Map exact headers to indexes and fail loudly if a source column is absent."""
    indexes = {header: index for index, header in enumerate(headers) if header}
    missing = [header for header in required_headers if header not in indexes]
    if missing:
        raise KmParseError(f"missing workbook headers: {missing}")
    return {header: indexes[header] for header in required_headers}


def alias_header_index(headers: list[str], aliases: dict[str, tuple[str, ...]]) -> dict[str, int]:
    """Map canonical field names to source header indexes through known aliases."""
    normalized = {normalize_key(header): index for index, header in enumerate(headers) if header}
    result: dict[str, int] = {}
    missing: list[str] = []
    for canonical, choices in aliases.items():
        found = next((normalized[normalize_key(choice)] for choice in choices if normalize_key(choice) in normalized), None)
        if found is None:
            missing.append(canonical)
        else:
            result[canonical] = found
    if missing:
        raise KmParseError(f"missing workbook headers: {missing}")
    return result
