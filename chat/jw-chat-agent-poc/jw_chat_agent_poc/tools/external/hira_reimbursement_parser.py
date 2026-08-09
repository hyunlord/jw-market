from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
import re
from typing import Final
from urllib.parse import urljoin, urlparse


_MAX_CRITERION_TEXT_LENGTH: Final[int] = 12_000
_DATE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?P<year>20\d{2})[.\-/](?P<month>\d{1,2})[.\-/](?P<day>\d{1,2})"
)
_NOTICE_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:보건복지부\s*)?고시\s*제?\s*20\d{2}\s*-\s*\d+\s*호"
)
_POPUP_CALL_RE: Final[re.Pattern[str]] = re.compile(
    r"viewInsuAdtCrtr\(\s*\d+\s*,\s*'(?P<date>\d{8})'\s*,\s*"
    r"'(?P<sno>\d+)'\s*,\s*'(?P<registration>\d+)'\s*,\s*'\d+'\s*\)"
)
_COMPOUND_PRODUCT_SUFFIX_RE: Final[str] = r"(?:주(?:사)?|피하주사)"
_DETAIL_CONTAINER_TOKENS: Final[frozenset[str]] = frozenset(
    {"bbs_view_cont", "board-view", "board_view", "view-content", "view_cont", "viewcont"}
)


@dataclass(frozen=True, slots=True)
class ReimbursementSearchRow:
    title: str
    url: str
    source_date: str | None
    notice_number: str | None


class _LinkTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[tuple[str, tuple[tuple[str, str], ...]]] = []
        self._in_row = False
        self._row_text: list[str] = []
        self._links: list[tuple[str, str]] = []
        self._link_href: str | None = None
        self._link_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        if tag == "tr":
            self._in_row = True
            self._row_text = []
            self._links = []
        elif self._in_row and tag == "a":
            self._link_href = _detail_href(attributes)
            self._link_text = []

    def handle_data(self, data: str) -> None:
        if self._in_row:
            self._row_text.append(data)
        if self._link_href is not None:
            self._link_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._link_href is not None:
            self._links.append((self._link_href, _compact_text(self._link_text)))
            self._link_href = None
            self._link_text = []
        elif tag == "tr" and self._in_row:
            self.rows.append((_compact_text(self._row_text), tuple(self._links)))
            self._in_row = False


class _CriterionTextParser(HTMLParser):
    _SKIP: Final[frozenset[str]] = frozenset(
        {"script", "style", "nav", "header", "footer"}
    )
    _VOID: Final[frozenset[str]] = frozenset(
        {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._container_depth = 0
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        if self._container_depth and tag not in self._VOID:
            self._container_depth += 1
        elif _is_detail_container(attributes):
            self._container_depth = 1
        if self._container_depth and tag in self._SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        # HTMLParser reports ``<br />`` as a start/end pair. Void elements never
        # increase the container depth, so decrementing here would close the
        # HIRA detail container at its first line break.
        if tag in self._VOID:
            return
        if self._container_depth and tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        if self._container_depth:
            self._container_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._container_depth and not self._skip_depth:
            self.parts.append(data)


def matching_search_row(
    html: str,
    brand: str,
    base_url: str,
) -> ReimbursementSearchRow | None:
    parser = _LinkTableParser()
    parser.feed(html)
    for row_text, links in parser.rows:
        if not _contains_brand_reference(row_text, brand):
            continue
        link = next(
            (
                (href, title)
                for href, title in links
                if href and title and _contains_brand_reference(title, brand)
            ),
            None,
        )
        if link is None:
            continue
        href, title = link
        date_match = _DATE_RE.search(row_text)
        notice_match = _NOTICE_RE.search(row_text)
        return ReimbursementSearchRow(
            title=title,
            url=urljoin(base_url, href),
            source_date=_normalized_date(date_match) if date_match else None,
            notice_number=_compact_notice(notice_match.group(0)) if notice_match else None,
        )
    return None


def detail_text(html: str) -> str:
    parser = _CriterionTextParser()
    parser.feed(html)
    text = _compact_text(parser.parts)
    if len(text) < 20:
        return ""
    return text[:_MAX_CRITERION_TEXT_LENGTH]


def is_official_hira_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" and parsed.hostname == "www.hira.or.kr"


def _compact_text(parts: list[str]) -> str:
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _normalized_date(match: re.Match[str]) -> str:
    return (
        f"{int(match.group('year')):04d}-"
        f"{int(match.group('month')):02d}-"
        f"{int(match.group('day')):02d}"
    )


def _compact_notice(value: str) -> str:
    return re.sub(r"\s+", " ", value).replace(" - ", "-").strip()


def _contains_brand_token(value: str, brand: str) -> bool:
    return (
        re.search(
            rf"(?<![0-9A-Za-z가-힣]){re.escape(brand)}(?![0-9A-Za-z가-힣])",
            value,
            flags=re.IGNORECASE,
        )
        is not None
    )


def _contains_brand_reference(value: str, brand: str) -> bool:
    if _contains_brand_token(value, brand):
        return True
    return (
        re.search(
            rf"(?<![0-9A-Za-z가-힣]){re.escape(brand)}{_COMPOUND_PRODUCT_SUFFIX_RE}",
            value,
            flags=re.IGNORECASE,
        )
        is not None
    )


def _detail_href(attributes: dict[str, str]) -> str:
    href = attributes.get("href", "")
    if href and href != "#none":
        return href
    match = _POPUP_CALL_RE.search(attributes.get("onclick", ""))
    if match is None:
        return href
    return (
        "/rc/insu/insuadtcrtr/InsuAdtCrtrPopup.do"
        f"?mtgHmeDd={match.group('date')}"
        f"&sno={match.group('sno')}"
        f"&mtgMtrRegSno={match.group('registration')}"
    )


def _is_detail_container(attributes: dict[str, str]) -> bool:
    tokens = {
        token.casefold()
        for key in ("class", "id")
        for token in attributes.get(key, "").split()
    }
    return bool(tokens & _DETAIL_CONTAINER_TOKENS)
