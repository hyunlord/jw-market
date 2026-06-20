"""Read JW Meeting workbooks into append-preserving stage events."""

from __future__ import annotations

from pathlib import Path

import openpyxl

from pipeline.scripts.etl.brand_activity.km_core import (
    CellValue,
    MeetingEvent,
    MessageCountCell,
    alias_header_index,
    normalize_spaces,
    normalize_text,
    parse_date_iso,
    parse_nullable_int,
    read_header_row,
    read_message_count_cells,
    row_is_empty,
    source_sha256,
)


MEETING_ALIASES: dict[str, tuple[str, ...]] = {
    "meeting_date": ("Meeting date",),
    "meeting_topic": ("Meeting Topic",),
    "meeting_format": ("Meeting Format",),
    "pharma_sponsor": ("Pharmaceutical Sponsor",),
    "non_pharma_sponsor": ("Non-Pharmaceutical Sponsor",),
    "no_at_meeting": ("No at Meeting",),
    "product_name": ("Product name", "PRODUCT NAME"),
    "therapeutic_class": ("Therapeutic Class",),
    "prescription_frequency": ("Prescription frequency text", "Prescription frequency"),
    "prescription_evolution": ("Prescription change text", "Prescription evolution"),
    "interest": ("Interest (Information Presented)", "Interest"),
    "verbatim_message": ("Verbatim Message (Information Caught Attention)", "Verbatim Message"),
    "other_comments": ("Other Comments",),
}


def _cell(values: tuple[CellValue, ...], index: int) -> str:
    """Return normalized text for a known Meeting source column."""
    return normalize_spaces(normalize_text(values[index]))


def read_meeting_events(workbook_path: Path) -> list[MeetingEvent]:
    """Read `Meetings` rows exactly once each, preserving duplicate events."""
    workbook = openpyxl.load_workbook(workbook_path, read_only=True, data_only=True)
    try:
        sheet = workbook["Meetings"]
        headers = read_header_row(sheet, max_col=40)
        indexes = alias_header_index(headers, MEETING_ALIASES)
        workbook_hash = source_sha256(workbook_path)
        rows: list[MeetingEvent] = []
        for source_row_no, values in enumerate(
            sheet.iter_rows(min_row=2, max_col=len(headers), values_only=True),
            start=2,
        ):
            if row_is_empty(values):
                continue
            meeting_date = parse_date_iso(values[indexes["meeting_date"]])
            rows.append(
                MeetingEvent(
                    meeting_date=meeting_date,
                    period_ym=meeting_date[:7],
                    meeting_topic=_cell(values, indexes["meeting_topic"]),
                    meeting_format=_cell(values, indexes["meeting_format"]),
                    pharma_sponsor=_cell(values, indexes["pharma_sponsor"]),
                    non_pharma_sponsor=_cell(values, indexes["non_pharma_sponsor"]),
                    no_at_meeting=parse_nullable_int(values[indexes["no_at_meeting"]]),
                    product_name=_cell(values, indexes["product_name"]),
                    therapeutic_class=_cell(values, indexes["therapeutic_class"]),
                    prescription_frequency=_cell(values, indexes["prescription_frequency"]),
                    prescription_evolution=_cell(values, indexes["prescription_evolution"]),
                    interest=_cell(values, indexes["interest"]),
                    verbatim_message=_cell(values, indexes["verbatim_message"]),
                    other_comments=_cell(values, indexes["other_comments"]),
                    source_file=workbook_path.name,
                    source_sheet="Meetings",
                    source_row_no=source_row_no,
                    source_file_sha256=workbook_hash,
                )
            )
        return rows
    finally:
        workbook.close()


def read_meeting_message_counts(workbook_path: Path) -> list[MessageCountCell]:
    """Read validation-only Meeting Message Count cells."""
    return read_message_count_cells(workbook_path, "meeting")
