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
        html = fetch_form(index_url, page_form(page))
        current_total = parse_total_count(html)
        if total_count is None:
            total_count = current_total
            total_pages = math.ceil(total_count / PAGE_SIZE)
        elif current_total != total_count:
            raise RuntimeError(
                f"HIRA index changed during pagination: {total_count}->{current_total}"
            )
        rows = parse_list_html(html, base_url=base_url)
        expected = (
            total_count - PAGE_SIZE * (total_pages - 1)
            if page == total_pages
            else PAGE_SIZE
        )
        if len(rows) != expected:
            raise RuntimeError(
                f"HIRA page row gap: page={page} rows={len(rows)} expected={expected}"
            )
        pages.append(rows)
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
