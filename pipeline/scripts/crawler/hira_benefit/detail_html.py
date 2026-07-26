from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser


def _clean(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split())


@dataclass(frozen=True, slots=True)
class DetailDocument:
    raw_text: str
    headings: tuple[tuple[str, str, list[str]], ...]
    blocks: tuple[str, ...]
    table_rows: tuple[tuple[str, ...], ...]
    title: str | None
    structural_html_valid: bool
    content_container_present: bool


class _DetailParser(HTMLParser):
    # Track only the required page shell. Optional content markup is often
    # imperfect in historical notices and must not turn valid pages into FAILED.
    _STRUCTURAL_TAGS = frozenset({"html", "body", "main", "h1"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.visible: list[str] = []
        self.headings: list[tuple[str, str, list[str]]] = []
        self.blocks: list[str] = []
        self.table_rows: list[tuple[str, ...]] = []
        self.title_parts: list[str] = []
        self._heading_level: str | None = None
        self._heading_parts: list[str] = []
        self._current_section: list[str] | None = None
        self._block_tag: str | None = None
        self._block_parts: list[str] = []
        self._table_row: list[str] | None = None
        self._table_cell: list[str] | None = None
        self._ignored_depth = 0
        self._structural_stack: list[str] = []
        self._structural_error = False
        self._content_container_present = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.lower()
        if lowered in self._STRUCTURAL_TAGS:
            self._structural_stack.append(lowered)
        if lowered in {"body", "main"}:
            self._content_container_present = True
        if lowered in {"script", "style", "noscript"}:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if lowered in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._heading_level = lowered
            self._heading_parts = []
        if lowered in {"p", "li"}:
            self._block_tag = lowered
            self._block_parts = []
        if lowered == "tr":
            self._table_row = []
        if lowered in {"th", "td"} and self._table_row is not None:
            self._table_cell = []

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        value = _clean(data)
        if not value:
            return
        self.visible.append(value)
        if self._heading_level is not None:
            self._heading_parts.append(value)
        elif self._current_section is not None:
            self._current_section.append(value)
        if self._block_tag is not None:
            self._block_parts.append(value)
        if self._table_cell is not None:
            self._table_cell.append(value)

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered in self._STRUCTURAL_TAGS:
            if not self._structural_stack or self._structural_stack[-1] != lowered:
                self._structural_error = True
            else:
                self._structural_stack.pop()
        if lowered in {"script", "style", "noscript"} and self._ignored_depth:
            self._ignored_depth -= 1
            return
        if lowered == self._heading_level:
            heading = _clean(" ".join(self._heading_parts))
            values: list[str] = []
            self.headings.append((lowered, heading, values))
            self._current_section = values
            if lowered == "h1" and not self.title_parts:
                self.title_parts.append(heading)
            self._heading_level = None
            self._heading_parts = []
        if lowered == self._block_tag:
            block = _clean(" ".join(self._block_parts))
            if block:
                self.blocks.append(block)
            self._block_tag = None
            self._block_parts = []
        if lowered in {"th", "td"} and self._table_cell is not None:
            self._table_row.append(_clean(" ".join(self._table_cell)))
            self._table_cell = None
        if lowered == "tr" and self._table_row is not None:
            cells = tuple(value for value in self._table_row if value)
            if cells:
                self.table_rows.append(cells)
            self._table_row = None

    @property
    def structural_html_valid(self) -> bool:
        return not self._structural_error and not self._structural_stack

    @property
    def content_container_present(self) -> bool:
        return self._content_container_present


def parse_detail_document(html: str) -> DetailDocument:
    parser = _DetailParser()
    parser.feed(html)
    parser.close()
    return DetailDocument(
        raw_text=_clean(" ".join(parser.visible)),
        headings=tuple(parser.headings),
        blocks=tuple(parser.blocks),
        table_rows=tuple(parser.table_rows),
        title=parser.title_parts[0] if parser.title_parts else None,
        structural_html_valid=parser.structural_html_valid,
        content_container_present=parser.content_container_present,
    )
