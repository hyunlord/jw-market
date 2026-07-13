from __future__ import annotations

import io
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import fitz
import pytest
from fastapi import UploadFile

from src import settings, upload_adapter
from src.upload_adapter import SavedTempDocument


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _upload(name: str, size_bytes: int) -> UploadFile:
    return UploadFile(filename=name, file=io.BytesIO(b"0" * size_bytes))


def _write_pdf(path: Path, page_text: list[str]) -> None:
    document = fitz.open()
    for text in page_text:
        page = document.new_page()
        if text:
            page.insert_text((72, 72), text)
    document.save(path)
    document.close()


def test_external_preprocessor_upload_gate_allows_ada_sized_pdf(monkeypatch) -> None:
    monkeypatch.setattr(settings, "EXTERNAL_PREPROCESSOR_MAX_FILE_MB", 100)
    pdf = _upload("ada.pdf", 59_097_189)

    blocked = upload_adapter.blocked_external_preprocessor_uploads([pdf])

    assert blocked == []


def test_external_preprocessor_upload_gate_blocks_over_global_limit(monkeypatch) -> None:
    monkeypatch.setattr(settings, "EXTERNAL_PREPROCESSOR_MAX_FILE_MB", 100)
    pdf = _upload("too-large.pdf", (100 * 1024 * 1024) + 1)

    blocked = upload_adapter.blocked_external_preprocessor_uploads([pdf])

    assert len(blocked) == 1
    assert blocked[0].route == "blocked_oversized"
    assert blocked[0].route_reason == "PDF·PPTX 파일은 최대 100MB까지 지원됩니다."


def test_external_preprocessor_upload_gate_allows_safe_pdf_and_ignores_xlsx(monkeypatch) -> None:
    monkeypatch.setattr(settings, "EXTERNAL_PREPROCESSOR_MAX_FILE_MB", 100)
    safe_pdf = _upload("safe.pdf", 6 * 1024 * 1024)
    large_xlsx = _upload("large.xlsx", 60 * 1024 * 1024)

    blocked = upload_adapter.blocked_external_preprocessor_uploads([safe_pdf, large_xlsx])

    assert blocked == []


def test_saved_document_gate_allows_328_page_text_layer_pdf(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "EXTERNAL_PREPROCESSOR_MAX_FILE_MB", 100)
    monkeypatch.setattr(settings, "EXTERNAL_PREPROCESSOR_MAX_PDF_PAGES", 0)
    monkeypatch.setattr(settings, "PDF_TEXT_LAYER_MIN_CHARS", 20)
    monkeypatch.setattr(settings, "PDF_TEXT_PAGE_SECONDS", 0.38)
    monkeypatch.setattr(settings, "PDF_OCR_PAGE_SECONDS", 3.91)
    monkeypatch.setattr(settings, "PDF_MAX_ESTIMATED_SECONDS", 300.0)
    pdf_path = tmp_path / "ada-like.pdf"
    _write_pdf(pdf_path, ["embedded ADA guideline text with sufficient characters"] * 328)
    document = SavedTempDocument(temp_document_id=1, file_name="ada-like.pdf", file_path=str(pdf_path))

    blocked = upload_adapter.blocked_saved_external_preprocessor_documents([document])

    assert blocked == []


def test_saved_document_gate_honors_optional_emergency_page_cap(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "EXTERNAL_PREPROCESSOR_MAX_PDF_PAGES", 1)
    monkeypatch.setattr(settings, "PDF_TEXT_LAYER_MIN_CHARS", 20)
    pdf_path = tmp_path / "two-pages.pdf"
    _write_pdf(pdf_path, ["native text layer with sufficient characters"] * 2)
    document = SavedTempDocument(temp_document_id=1, file_name=pdf_path.name, file_path=str(pdf_path))

    blocked = upload_adapter.blocked_saved_external_preprocessor_documents([document])

    assert len(blocked) == 1
    assert blocked[0].route_reason == "PDF는 최대 1페이지까지 지원됩니다."


def test_saved_document_gate_profiles_mixed_pdf_page_by_page(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(settings, "PDF_TEXT_LAYER_MIN_CHARS", 20)
    monkeypatch.setattr(settings, "PDF_TEXT_PAGE_SECONDS", 0.38)
    monkeypatch.setattr(settings, "PDF_OCR_PAGE_SECONDS", 3.91)
    pdf_path = tmp_path / "mixed.pdf"
    _write_pdf(pdf_path, (["native text layer with sufficient characters"] * 3) + ([""] * 2))

    estimate = upload_adapter._pdf_processing_estimate(pdf_path)

    assert estimate is not None
    assert estimate.page_count == 5
    assert estimate.text_layer_pages == 3
    assert estimate.ocr_candidate_pages == 2
    assert estimate.estimated_seconds == pytest.approx((3 * 0.38) + (2 * 3.91))


def test_saved_document_gate_allows_60_page_scan_within_budget(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(settings, "EXTERNAL_PREPROCESSOR_MAX_PDF_PAGES", 0)
    monkeypatch.setattr(settings, "PDF_TEXT_LAYER_MIN_CHARS", 20)
    monkeypatch.setattr(settings, "PDF_TEXT_PAGE_SECONDS", 0.38)
    monkeypatch.setattr(settings, "PDF_OCR_PAGE_SECONDS", 3.91)
    monkeypatch.setattr(settings, "PDF_MAX_ESTIMATED_SECONDS", 300.0)
    pdf_path = tmp_path / "scan-60.pdf"
    _write_pdf(pdf_path, [""] * 60)
    document = SavedTempDocument(temp_document_id=1, file_name=pdf_path.name, file_path=str(pdf_path))

    blocked = upload_adapter.blocked_saved_external_preprocessor_documents([document])

    assert blocked == []


def test_saved_document_gate_rejects_scan_when_estimated_time_exceeds_budget(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(settings, "EXTERNAL_PREPROCESSOR_MAX_PDF_PAGES", 0)
    monkeypatch.setattr(settings, "PDF_TEXT_LAYER_MIN_CHARS", 20)
    monkeypatch.setattr(settings, "PDF_TEXT_PAGE_SECONDS", 0.38)
    monkeypatch.setattr(settings, "PDF_OCR_PAGE_SECONDS", 3.91)
    monkeypatch.setattr(settings, "PDF_MAX_ESTIMATED_SECONDS", 300.0)
    pdf_path = tmp_path / "scan-300.pdf"
    _write_pdf(pdf_path, [""] * 300)
    document = SavedTempDocument(temp_document_id=1, file_name=pdf_path.name, file_path=str(pdf_path))

    blocked = upload_adapter.blocked_saved_external_preprocessor_documents([document])

    assert len(blocked) == 1
    assert blocked[0].route == "blocked_oversized"
    assert "300페이지" in blocked[0].route_reason
    assert "OCR 예상 300페이지" in blocked[0].route_reason
    assert "1173초" in blocked[0].route_reason
    assert "300초" in blocked[0].route_reason


def test_saved_document_gate_rejects_pdf_when_admission_profile_fails(tmp_path: Path) -> None:
    pdf_path = tmp_path / "broken.pdf"
    pdf_path.write_bytes(b"not-a-pdf")
    document = SavedTempDocument(temp_document_id=1, file_name=pdf_path.name, file_path=str(pdf_path))

    blocked = upload_adapter.blocked_saved_external_preprocessor_documents([document])

    assert len(blocked) == 1
    assert blocked[0].route == "preprocess_failed"
    assert "PDF 페이지와 텍스트 레이어를 확인할 수 없습니다" in blocked[0].user_message


def test_admission_threshold_falls_back_to_legacy_p91_env() -> None:
    env = os.environ.copy()
    env.pop("PDF_TEXT_LAYER_MIN_CHARS", None)
    env["PDF_OCR_TEXT_MIN_NONSPACE"] = "137"

    output = subprocess.check_output(
        [
            sys.executable,
            "-c",
            "from src import settings; print(settings.PDF_TEXT_LAYER_MIN_CHARS)",
        ],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
    )

    assert output.strip() == "137"


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
    assert blocked[0].route_reason == "PPTX는 최대 120슬라이드까지 지원됩니다."
