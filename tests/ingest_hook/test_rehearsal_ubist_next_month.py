from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.etl.io.ubist_loader import iter_xlsx_rows
from pipeline.scripts.ingest_hook.rehearsal_ubist_next_month import (
    write_rehearsal_workbook,
)
from pipeline.scripts.ingest_hook.workbook_contracts import classify


def test_write_rehearsal_workbook_creates_loader_compatible_next_month(
    tmp_path: Path,
) -> None:
    output = tmp_path / "renamed-source.xlsx"

    report = write_rehearsal_workbook(output, "2026-06")
    rows = list(iter_xlsx_rows(output))

    assert report.period == "2026-06"
    assert report.rows == 3
    assert classify(output, "2026-06") == "ubist"
    assert len(rows) == 3
    assert {period for period, _ in rows} == {"2026-06"}
    assert {row["ATC"] for _, row in rows} == {"C10AA", "C10BA"}
    assert {row["브랜드"] for _, row in rows} == {
        "리바로",
        "리바로젯",
        "테스트대조",
    }


def test_write_rehearsal_workbook_rejects_non_month_epoch(
    tmp_path: Path,
) -> None:
    output = tmp_path / "invalid.xlsx"

    with pytest.raises(ValueError, match="invalid UBIST month"):
        write_rehearsal_workbook(output, "2026-Q2")

    assert not output.exists()
