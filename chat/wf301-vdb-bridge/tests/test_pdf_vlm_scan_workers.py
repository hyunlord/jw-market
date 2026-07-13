from __future__ import annotations

from pathlib import Path

import fitz

from src import pdf_vlm, settings


def _write_text_pdf(path: Path, page_count: int) -> None:
    with fitz.open() as document:
        for index in range(page_count):
            page = document.new_page()
            page.insert_text((72, 72), f"Native text page {index + 1}")
        document.save(path)


def test_parallel_scan_preserves_serial_page_decisions(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "ordered.pdf"
    _write_text_pdf(path, 11)
    monkeypatch.setattr(settings, "PDF_VLM_SCAN_WORKERS", 1, raising=False)
    serial = pdf_vlm.scan_pdf(path)

    monkeypatch.setattr(settings, "PDF_VLM_SCAN_WORKERS", 4, raising=False)
    parallel = pdf_vlm.scan_pdf(path)

    assert parallel.file_sha256 == serial.file_sha256
    assert parallel.page_count == serial.page_count
    assert parallel.pages == serial.pages
    assert [page.page_number for page in parallel.pages] == list(range(1, 12))


def test_parallel_scan_partitions_the_document(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "partitioned.pdf"
    _write_text_pdf(path, 10)
    calls: list[tuple[int, int, int]] = []

    def fake_scan_range(
        _path: Path,
        start: int,
        stop: int,
        page_count: int,
    ) -> list[pdf_vlm.PageDecision]:
        calls.append((start, stop, page_count))
        return [
            pdf_vlm.PageDecision(
                page_number=index + 1,
                decision="native_only",
                reason="native_text",
                native_nonspace_chars=1,
                image_count=0,
                largest_image_coverage=0.0,
                summed_image_coverage=0.0,
                drawing_count=0,
            )
            for index in range(start, stop)
        ]

    monkeypatch.setattr(settings, "PDF_VLM_SCAN_WORKERS", 3, raising=False)
    monkeypatch.setattr(pdf_vlm, "_scan_page_range", fake_scan_range, raising=False)

    scan = pdf_vlm.scan_pdf(path)

    assert sorted(calls) == [(0, 4, 10), (4, 7, 10), (7, 10, 10)]
    assert [page.page_number for page in scan.pages] == list(range(1, 11))
