from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .models import NoticeListItem
from .parser import parse_list_html

PAGE_SIZE = 30
_TOTAL_RE = re.compile(
    r"전체\s*:\s*<span[^>]*class=[\"']fcO[\"'][^>]*>\s*([0-9,]+)\s*</span>건"
)
FormFetcher = Callable[[str, Mapping[str, str]], str]


@dataclass(frozen=True, slots=True)
class NoticeIndex:
    items: tuple[NoticeListItem, ...]
    total_count: int
    total_pages: int
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class PageFetch:
    """One list page, already row-count checked against the reported total."""

    page: int
    total_count: int
    total_pages: int
    items: tuple[NoticeListItem, ...]


def total_pages_for(total_count: int) -> int:
    return math.ceil(total_count / PAGE_SIZE)


def expected_rows_for_page(page: int, *, total_count: int, total_pages: int) -> int:
    """Rows a given page must carry for the enumeration to be gap-free."""

    if page < 1 or page > total_pages:
        raise ValueError(f"page {page} is outside 1..{total_pages}")
    if page == total_pages:
        return total_count - PAGE_SIZE * (total_pages - 1)
    return PAGE_SIZE


def fetch_page(
    page: int,
    *,
    index_url: str,
    base_url: str,
    fetch_form: FormFetcher,
) -> PageFetch:
    """Fetch a single list page and fail closed on a row-count gap.

    Splitting enumeration across activities means no single caller sees every
    page, so each page must be self-validating at the point it is fetched.
    """

    if page < 1:
        raise ValueError("page must be positive")
    html = fetch_form(index_url, page_form(page))
    total_count = parse_total_count(html)
    total_pages = total_pages_for(total_count)
    if page > total_pages:
        raise RuntimeError(
            f"HIRA page out of range: page={page} total_pages={total_pages}"
        )
    rows = parse_list_html(html, base_url=base_url)
    expected = expected_rows_for_page(
        page,
        total_count=total_count,
        total_pages=total_pages,
    )
    if len(rows) != expected:
        raise RuntimeError(
            f"HIRA page row gap: page={page} rows={len(rows)} expected={expected}"
        )
    return PageFetch(
        page=page,
        total_count=total_count,
        total_pages=total_pages,
        items=tuple(rows),
    )


def page_form(page: int) -> dict[str, str]:
    return {
        "pageIndex": str(page),
        "tabGbn": "01",
        "mtgHmeDd": "",
        "RN": "",
        "seqListYn": "N",
        "seqList": "",
        "searchYn": "",
        "allViewYn": "",
        "decIteTpCd": "01",
        "startDate": "",
        "endDate": "",
        "recordCountPerPage": str(PAGE_SIZE),
        "searchKeyword": "",
        "searchCondition": "TXTALL",
        "searchWord": "",
        "searchKeyword2": "",
    }


def parse_total_count(html: str) -> int:
    match = _TOTAL_RE.search(html)
    if match is None:
        raise RuntimeError("HIRA index total count is missing")
    return int(match.group(1).replace(",", ""))


def fetch_notice_index(
    *,
    index_url: str,
    base_url: str,
    fetch_form: FormFetcher,
) -> NoticeIndex:
    """Fetch every HIRA list page and fail closed on gaps or duplicates."""

    pages: list[tuple[NoticeListItem, ...]] = []
    total_count: int | None = None
    total_pages: int | None = None
    page = 1
    while total_pages is None or page <= total_pages:
        fetched = fetch_page(
            page,
            index_url=index_url,
            base_url=base_url,
            fetch_form=fetch_form,
        )
        if total_count is None:
            total_count = fetched.total_count
            total_pages = fetched.total_pages
        elif fetched.total_count != total_count:
            raise RuntimeError(
                "HIRA index changed during pagination: "
                f"{total_count}->{fetched.total_count}"
            )
        pages.append(fetched.items)
        page += 1

    if total_count is None or total_pages is None:
        raise RuntimeError("HIRA index pagination produced no pages")
    items = tuple(item for rows in pages for item in rows)
    identities = {item.source_notice_id for item in items}
    if len(items) != total_count or len(identities) != total_count:
        raise RuntimeError(
            "HIRA manifest identity gap: "
            f"items={len(items)} unique={len(identities)} total={total_count}"
        )
    from .backfill import build_backfill_manifest

    return NoticeIndex(
        items=items,
        total_count=total_count,
        total_pages=total_pages,
        manifest_sha256=build_backfill_manifest(
            items,
            chunk_size=max(1, total_count),
        ).manifest_sha256,
    )
