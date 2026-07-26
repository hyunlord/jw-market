from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from .models import NoticeListItem, ParsedNotice
from .typed_extraction import extract_structured

_DATE_RE = re.compile(r"\b(20\d{2})[-./년]\s*(\d{1,2})[-./월]\s*(\d{1,2})일?\b")
_NOTICE_RE = re.compile(r"(?:고시\s*)?(제\s*\d{4}\s*-\s*\d+\s*호)")
_POPUP_CALL_RE = re.compile(
    r"viewInsuAdtCrtr\(\s*\d+\s*,\s*['\"](\d{8})['\"]\s*,"
    r"\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]"
)


def _clean(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split())


def _parse_date(value: str) -> date | None:
    match = _DATE_RE.search(value)
    if match is None:
        return None
    try:
        return date(*(int(part) for part in match.groups()))
    except ValueError:
        return None


@dataclass
class _Row:
    texts: list[str] = field(default_factory=list)
    hrefs: list[str] = field(default_factory=list)
    popup_calls: list[str] = field(default_factory=list)
    link_texts: dict[str, list[str]] = field(default_factory=dict)


class _ListParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[_Row] = []
        self._row: _Row | None = None
        self._active_href: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "tr":
            self._row = _Row()
        if self._row is not None and tag.lower() == "a":
            attributes = dict(attrs)
            href = attributes.get("href")
            onclick = attributes.get("onclick")
            link_key = None
            if href:
                self._row.hrefs.append(href)
                self._row.link_texts.setdefault(href, [])
                link_key = href
            if onclick and _POPUP_CALL_RE.search(onclick):
                self._row.popup_calls.append(onclick)
                self._row.link_texts.setdefault(onclick, [])
                link_key = onclick
            self._active_href = link_key

    def handle_data(self, data: str) -> None:
        if self._row is not None and _clean(data):
            self._row.texts.append(_clean(data))
            if self._active_href is not None:
                self._row.link_texts[self._active_href].append(_clean(data))

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None
        if tag.lower() == "a":
            self._active_href = None


def parse_list_html(html: str, *, base_url: str) -> tuple[NoticeListItem, ...]:
    parser = _ListParser()
    parser.feed(html)
    result: list[NoticeListItem] = []
    seen: set[str] = set()
    for row in parser.rows:
        href = next((value for value in row.hrefs if "brdBltNo=" in value), None)
        popup_call = row.popup_calls[0] if row.popup_calls else None
        link_key = href
        if href is not None:
            notice_id = (
                parse_qs(urlparse(href).query).get("brdBltNo", [""])[0].strip()
            )
            source_url = urljoin(base_url, href)
        elif popup_call is not None:
            popup_match = _POPUP_CALL_RE.search(popup_call)
            if popup_match is None:
                continue
            meeting_date, sequence, registration_sequence = popup_match.groups()
            notice_id = f"{meeting_date}-{sequence}-{registration_sequence}"
            source_url = urljoin(
                base_url,
                "/rc/insu/insuadtcrtr/InsuAdtCrtrPopup.do?"
                + urlencode(
                    {
                        "mtgHmeDd": meeting_date,
                        "sno": sequence,
                        "mtgMtrRegSno": registration_sequence,
                    }
                ),
            )
            link_key = popup_call
        else:
            continue
        row_text = _clean(" ".join(row.texts))
        notice_date = _parse_date(row_text)
        if not notice_id or notice_date is None or notice_id in seen:
            continue
        title = _clean(" ".join(row.link_texts.get(link_key or "", []))) or notice_id
        seen.add(notice_id)
        result.append(
            NoticeListItem.create(
                source_notice_id=notice_id,
                title=title,
                notice_date=notice_date,
                source_url=source_url,
            )
        )
    return tuple(result)


class _DetailParser(HTMLParser):
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

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.lower()
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


def parse_detail_html(
    html: str,
    *,
    source_notice_id: str,
    source_url: str,
) -> ParsedNotice:
    parser = _DetailParser()
    parser.feed(html)
    raw_text = _clean(" ".join(parser.visible))
    structured = extract_structured(
        raw_text=raw_text,
        headings=parser.headings,
        table_rows=parser.table_rows,
        blocks=parser.blocks,
    )
    notice_match = _NOTICE_RE.search(raw_text)
    notice_no = (
        re.sub(r"\s+", "", notice_match.group(1))
        if notice_match is not None
        else None
    )
    return ParsedNotice(
        source_notice_id=source_notice_id,
        source_url=source_url,
        title=parser.title_parts[0] if parser.title_parts else None,
        notice_no=notice_no,
        notice_date=_parse_date(raw_text),
        target_condition=structured.target_condition,
        exclusion_rule=structured.exclusion_rule,
        dosage_limit=structured.dosage_limit,
        raw_text=raw_text,
        raw_html_sha256=hashlib.sha256(html.encode("utf-8")).hexdigest(),
        parse_status=structured.parse_status,
        failed_fields=structured.failed_fields,
    )
