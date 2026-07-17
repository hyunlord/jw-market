"""Build a separately-owned leading-page PDF index for progressive search."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import fitz

from .upload_adapter import SavedTempDocument

_PREVIEW_ID_BASE = 1_500_000_000
_PREVIEW_ID_SPACE = 500_000_000


@dataclass(frozen=True, slots=True)
class PdfPreviewArtifact:
    saved_document: SavedTempDocument
    indexed_pages: int
    total_pages: int
    source_temp_document_id: int | None = None


def preview_temp_document_id(upload_id: str, source_temp_document_id: int) -> int:
    """Return a stable high-range ID outside ordinary temp-document sequences."""

    digest = hashlib.sha256(
        f"wf301-pdf-preview:{upload_id}:{source_temp_document_id}".encode("utf-8")
    ).digest()
    return _PREVIEW_ID_BASE + int.from_bytes(digest[:8], "big") % _PREVIEW_ID_SPACE


def build_pdf_preview(
    source: SavedTempDocument,
    *,
    upload_id: str,
    max_pages: int,
) -> PdfPreviewArtifact | None:
    """Copy the leading page window without changing the original temp document."""

    if max_pages < 1 or Path(source.file_name).suffix.casefold() != ".pdf":
        return None
    source_path = Path(source.file_path)
    preview_id = preview_temp_document_id(upload_id, source.temp_document_id)
    preview_path = source_path.with_name(
        f".preview_TEMP_DOCUMENT_{preview_id}_{source_path.name}"
    )
    temporary_path = preview_path.with_suffix(f"{preview_path.suffix}.tmp")
    try:
        with fitz.open(source_path) as source_document:
            total_pages = len(source_document)
            if total_pages <= max_pages:
                return None
            indexed_pages = min(max_pages, total_pages)
            preview_document = fitz.open()
            try:
                preview_document.insert_pdf(
                    source_document,
                    from_page=0,
                    to_page=indexed_pages - 1,
                )
                preview_document.save(temporary_path)
            finally:
                preview_document.close()
        temporary_path.chmod(0o600)
        temporary_path.replace(preview_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        preview_path.unlink(missing_ok=True)
        raise
    return PdfPreviewArtifact(
        saved_document=SavedTempDocument(
            temp_document_id=preview_id,
            file_name=source.file_name,
            file_path=str(preview_path),
        ),
        indexed_pages=indexed_pages,
        total_pages=total_pages,
        source_temp_document_id=source.temp_document_id,
    )
