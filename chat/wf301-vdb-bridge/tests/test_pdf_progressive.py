from __future__ import annotations

from pathlib import Path

import fitz

from src.pdf_progressive import build_pdf_preview, preview_temp_document_id
from src.upload_adapter import SavedTempDocument


def _write_pdf(path: Path, pages: int) -> None:
    document = fitz.open()
    for number in range(1, pages + 1):
        page = document.new_page()
        page.insert_text((72, 72), f"page {number}")
    document.save(path)
    document.close()


def test_preview_id_is_stable_and_separate_from_real_temp_document_id() -> None:
    first = preview_temp_document_id("upl_7Qz4R4R2Xh9pCkN8", 11)
    second = preview_temp_document_id("upl_7Qz4R4R2Xh9pCkN8", 11)

    assert first == second
    assert 1_500_000_000 <= first < 2_000_000_000
    assert first != 11


def test_build_pdf_preview_copies_only_leading_pages_and_preserves_source_name(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "TEMP_DOCUMENT_11.pdf"
    _write_pdf(source_path, pages=30)
    source = SavedTempDocument(11, "market-report.pdf", str(source_path))

    preview = build_pdf_preview(
        source,
        upload_id="upl_7Qz4R4R2Xh9pCkN8",
        max_pages=10,
    )

    assert preview is not None
    assert preview.indexed_pages == 10
    assert preview.total_pages == 30
    assert preview.source_temp_document_id == source.temp_document_id
    assert preview.saved_document.file_name == "market-report.pdf"
    assert f"TEMP_DOCUMENT_{preview.saved_document.temp_document_id}" in Path(
        preview.saved_document.file_path
    ).name
    with fitz.open(preview.saved_document.file_path) as document:
        assert len(document) == 10
        assert "page 1" in document[0].get_text()
        assert "page 10" in document[9].get_text()


def test_short_pdf_skips_preview_because_full_run_is_already_the_first_window(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "TEMP_DOCUMENT_11.pdf"
    _write_pdf(source_path, pages=10)

    preview = build_pdf_preview(
        SavedTempDocument(11, "short.pdf", str(source_path)),
        upload_id="upl_7Qz4R4R2Xh9pCkN8",
        max_pages=10,
    )

    assert preview is None
