"""DOCX-specific text extraction for wf301 uploaded documents."""

from __future__ import annotations

from pathlib import Path
from typing import Final

from docx import Document
from docx.opc.exceptions import PackageNotFoundError


DEFAULT_CHUNK_CHAR_LIMIT: Final = 1800


class DocxPreprocessError(RuntimeError):
    """Raised when a DOCX file cannot be converted into searchable chunks."""


def extract_docx_chunks(path: Path, *, chunk_char_limit: int = DEFAULT_CHUNK_CHAR_LIMIT) -> list[str]:
    """Return paragraph and table-preserving chunks from a DOCX document."""
    if chunk_char_limit < 80:
        raise DocxPreprocessError("chunk_char_limit must be at least 80")
    try:
        document = Document(path)
        blocks = _document_blocks(document)
        chunks = _pack_chunks(blocks, chunk_char_limit)
    except (PackageNotFoundError, OSError, ValueError) as exc:
        raise DocxPreprocessError(f"docx preprocessing failed: {exc}") from exc
    if not chunks:
        raise DocxPreprocessError("docx preprocessing produced no chunks")
    return chunks


def _document_blocks(document: Document) -> list[str]:
    blocks: list[str] = []
    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if text:
            blocks.append(text)
    for table_index, table in enumerate(document.tables, start=1):
        rows: list[str] = []
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            line = " | ".join(cell for cell in cells if cell)
            if line:
                rows.append(line)
        if rows:
            blocks.append(f"표 {table_index}\n" + "\n".join(rows))
    return blocks


def _pack_chunks(blocks: list[str], chunk_char_limit: int) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_size = 0
    for block in blocks:
        for part in _split_block(block, chunk_char_limit):
            separator_size = 2 if current else 0
            if current and current_size + separator_size + len(part) > chunk_char_limit:
                chunks.append("\n\n".join(current))
                current = []
                current_size = 0
                separator_size = 0
            current.append(part)
            current_size += separator_size + len(part)
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def _split_block(block: str, chunk_char_limit: int) -> list[str]:
    if len(block) <= chunk_char_limit:
        return [block]
    lines = block.splitlines()
    parts: list[str] = []
    current: list[str] = []
    current_size = 0
    for line in lines:
        if len(line) > chunk_char_limit:
            if current:
                parts.append("\n".join(current))
                current = []
                current_size = 0
            parts.extend(_split_long_line(line, chunk_char_limit))
            continue
        separator_size = 1 if current else 0
        if current and current_size + separator_size + len(line) > chunk_char_limit:
            parts.append("\n".join(current))
            current = []
            current_size = 0
            separator_size = 0
        current.append(line)
        current_size += separator_size + len(line)
    if current:
        parts.append("\n".join(current))
    return parts


def _split_long_line(line: str, chunk_char_limit: int) -> list[str]:
    return [line[index : index + chunk_char_limit] for index in range(0, len(line), chunk_char_limit)]
