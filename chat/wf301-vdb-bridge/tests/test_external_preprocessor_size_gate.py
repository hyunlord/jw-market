from __future__ import annotations

import io
import zipfile
from pathlib import Path

from fastapi import UploadFile

from src import settings, upload_adapter
from src.upload_adapter import SavedTempDocument


def _upload(name: str, size_bytes: int) -> UploadFile:
    return UploadFile(filename=name, file=io.BytesIO(b"0" * size_bytes))


def test_external_preprocessor_upload_gate_blocks_large_pdf_before_delegation(monkeypatch) -> None:
    monkeypatch.setattr(settings, "EXTERNAL_PREPROCESSOR_MAX_FILE_MB", 20)
    pdf = _upload("large.pdf", (20 * 1024 * 1024) + 1)

    blocked = upload_adapter.blocked_external_preprocessor_uploads([pdf])

    assert len(blocked) == 1
    assert blocked[0].file_name == "large.pdf"
    assert blocked[0].route == "blocked_oversized"
    assert "exceeds PDF/PPTX preprocessor limit" in blocked[0].route_reason


def test_external_preprocessor_upload_gate_allows_safe_pdf_and_ignores_xlsx(monkeypatch) -> None:
    monkeypatch.setattr(settings, "EXTERNAL_PREPROCESSOR_MAX_FILE_MB", 20)
    safe_pdf = _upload("safe.pdf", 6 * 1024 * 1024)
    large_xlsx = _upload("large.xlsx", 60 * 1024 * 1024)

    blocked = upload_adapter.blocked_external_preprocessor_uploads([safe_pdf, large_xlsx])

    assert blocked == []


def test_saved_document_gate_blocks_pdf_page_count_without_preprocessor_call(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "EXTERNAL_PREPROCESSOR_MAX_FILE_MB", 20)
    monkeypatch.setattr(settings, "EXTERNAL_PREPROCESSOR_MAX_PDF_PAGES", 250)
    pdf_path = tmp_path / "many-pages.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n" + (b"/Type /Page\n" * 251))
    document = SavedTempDocument(temp_document_id=1, file_name="many-pages.pdf", file_path=str(pdf_path))

    blocked = upload_adapter.blocked_saved_external_preprocessor_documents([document])

    assert len(blocked) == 1
    assert blocked[0].file_name == "many-pages.pdf"
    assert "page_count=251" in blocked[0].route_reason


def test_saved_document_gate_blocks_pptx_slide_count_without_preprocessor_call(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "EXTERNAL_PREPROCESSOR_MAX_FILE_MB", 20)
    monkeypatch.setattr(settings, "EXTERNAL_PREPROCESSOR_MAX_PPTX_SLIDES", 120)
    pptx_path = tmp_path / "many-slides.pptx"
    with zipfile.ZipFile(pptx_path, "w") as archive:
        for index in range(1, 122):
            archive.writestr(f"ppt/slides/slide{index}.xml", "<p:sld/>")
    document = SavedTempDocument(temp_document_id=1, file_name="many-slides.pptx", file_path=str(pptx_path))

    blocked = upload_adapter.blocked_saved_external_preprocessor_documents([document])

    assert len(blocked) == 1
    assert blocked[0].file_name == "many-slides.pptx"
    assert "slide_count=121" in blocked[0].route_reason
