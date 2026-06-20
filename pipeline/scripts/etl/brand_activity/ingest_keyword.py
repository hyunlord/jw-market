"""Read JW Keyword workbooks into append-preserving stage events."""

from __future__ import annotations

from pathlib import Path

import openpyxl

from pipeline.scripts.etl.brand_activity.km_core import (
    CellValue,
    KeywordEvent,
    header_index,
    normalize_spaces,
    normalize_text,
    parse_period_ym,
    read_header_row,
    row_is_empty,
    source_sha256,
)
from pipeline.scripts.etl.brand_activity.km_message_count import MessageCountCell, read_message_count_cells


KEYWORD_HEADERS: tuple[str, ...] = (
    "Related date",
    "VISIT LOCATION",
    "SPECIALTY NAME",
    "REP# CO",
    "PRODUCT NAME",
    "THERAPEUTIC CLASS",
    "KEYWORDS",
    "INTEREST",
    "Prescription frequency",
    "Prescription evolution",
    "Abstract and clinical literature / data",
    "Patient educational literature",
    "Promotional product literature",
    "SAMPLES LEFT",
    "OTHER MATERIALS LEFT",
    "WHAT OTHER MATERIALS",
    "OTHER COMMENTS",
)


def _cell(values: tuple[CellValue, ...], index: int) -> str:
    """Return normalized text for a known Keyword source column."""
    return normalize_spaces(normalize_text(values[index]))


def read_keyword_events(workbook_path: Path) -> list[KeywordEvent]:
    """Read `Keywords` rows exactly once each, preserving duplicate events."""
    workbook = openpyxl.load_workbook(workbook_path, read_only=True, data_only=True)
    try:
        sheet = workbook["Keywords"]
        headers = read_header_row(sheet, max_col=40)
        indexes = header_index(headers, KEYWORD_HEADERS)
        workbook_hash = source_sha256(workbook_path)
        rows: list[KeywordEvent] = []
        for source_row_no, values in enumerate(
            sheet.iter_rows(min_row=2, max_col=len(headers), values_only=True),
            start=2,
        ):
            if row_is_empty(values):
                continue
            rows.append(
                KeywordEvent(
                    period_ym=parse_period_ym(values[indexes["Related date"]]),
                    visit_location=_cell(values, indexes["VISIT LOCATION"]),
                    specialty=_cell(values, indexes["SPECIALTY NAME"]),
                    representing_company=_cell(values, indexes["REP# CO"]),
                    product_name=_cell(values, indexes["PRODUCT NAME"]),
                    therapeutic_class=_cell(values, indexes["THERAPEUTIC CLASS"]),
                    keyword_text=_cell(values, indexes["KEYWORDS"]),
                    interest=_cell(values, indexes["INTEREST"]),
                    prescription_frequency=_cell(values, indexes["Prescription frequency"]),
                    prescription_evolution=_cell(values, indexes["Prescription evolution"]),
                    abstract_lit=_cell(values, indexes["Abstract and clinical literature / data"]),
                    patient_lit=_cell(values, indexes["Patient educational literature"]),
                    promotional_lit=_cell(values, indexes["Promotional product literature"]),
                    samples_left=_cell(values, indexes["SAMPLES LEFT"]),
                    other_materials_left=_cell(values, indexes["OTHER MATERIALS LEFT"]),
                    what_other_materials=_cell(values, indexes["WHAT OTHER MATERIALS"]),
                    other_comments=_cell(values, indexes["OTHER COMMENTS"]),
                    source_file=workbook_path.name,
                    source_sheet="Keywords",
                    source_row_no=source_row_no,
                    source_file_sha256=workbook_hash,
                )
            )
        return rows
    finally:
        workbook.close()


def read_keyword_message_counts(workbook_path: Path) -> list[MessageCountCell]:
    """Read validation-only Keyword Message Count cells."""
    return read_message_count_cells(workbook_path, "keyword")
