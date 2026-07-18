"""DOCX-specific text extraction for wf301 uploaded documents."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

from docx import Document
from docx.opc.exceptions import PackageNotFoundError
from docx.table import Table
from docx.text.paragraph import Paragraph


DEFAULT_CHUNK_CHAR_LIMIT: Final = 1800


class DocxPreprocessError(RuntimeError):
    """Raised when a DOCX file cannot be converted into searchable chunks."""


@dataclass(frozen=True)
class DocxChunkRecord:
    text: str
    section_title: str | None


@dataclass(frozen=True)
class _DocumentBlock:
    text: str
    section_title: str | None


def extract_docx_chunks(path: Path, *, chunk_char_limit: int = DEFAULT_CHUNK_CHAR_LIMIT) -> list[str]:
    """Return paragraph and table-preserving chunks from a DOCX document."""
    return [
        record.text
        for record in extract_docx_chunk_records(
            path,
            chunk_char_limit=chunk_char_limit,
        )
    ]


def extract_docx_chunk_records(
    path: Path,
    *,
    chunk_char_limit: int = DEFAULT_CHUNK_CHAR_LIMIT,
) -> list[DocxChunkRecord]:
    """Return searchable DOCX chunks together with their nearest heading."""
    if chunk_char_limit < 80:
        raise DocxPreprocessError("chunk_char_limit must be at least 80")
    try:
        document = Document(path)
        blocks = _document_blocks(document)
        chunks = _pack_chunk_records(blocks, chunk_char_limit)
    except (PackageNotFoundError, OSError, ValueError) as exc:
        raise DocxPreprocessError(f"docx preprocessing failed: {exc}") from exc
    if not chunks:
        raise DocxPreprocessError("docx preprocessing produced no chunks")
    return chunks


def _document_blocks(document: Document) -> list[_DocumentBlock]:
    blocks: list[_DocumentBlock] = []
    section_title: str | None = None
    table_index = 0
    for item in document.iter_inner_content():
        if isinstance(item, Paragraph):
            text = item.text.strip()
            if not text:
                continue
            if _is_heading(item):
                section_title = text
            blocks.append(_DocumentBlock(text=text, section_title=section_title))
            continue
        if not isinstance(item, Table):
            continue
        table_index += 1
        rows: list[str] = []
        for row in item.rows:
            cells = [cell.text.strip() for cell in row.cells]
            line = " | ".join(cell for cell in cells if cell)
            if line:
                rows.append(line)
        if rows:
            blocks.append(
                _DocumentBlock(
                    text=f"표 {table_index}\n" + "\n".join(rows),
                    section_title=section_title,
                )
            )
    return blocks


def _is_heading(paragraph: Paragraph) -> bool:
    style_name = str(getattr(paragraph.style, "name", "") or "").casefold()
    return style_name.startswith(("heading", "제목"))


def _pack_chunk_records(
    blocks: list[_DocumentBlock],
    chunk_char_limit: int,
) -> list[DocxChunkRecord]:
    chunks: list[DocxChunkRecord] = []
    current: list[str] = []
    current_size = 0
    current_section: str | None = None

    def flush() -> None:
        nonlocal current, current_size
        if current:
            chunks.append(
                DocxChunkRecord(
                    text="\n\n".join(current),
                    section_title=current_section,
                )
            )
        current = []
        current_size = 0

    for block in blocks:
        if current and block.section_title != current_section:
            flush()
        current_section = block.section_title
        for part in _split_block(block.text, chunk_char_limit):
            separator_size = 2 if current else 0
            if current and current_size + separator_size + len(part) > chunk_char_limit:
                flush()
                separator_size = 0
            current.append(part)
            current_size += separator_size + len(part)
    flush()
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
