"""Durable page receipts and the fail-closed discovery reducer.

Index enumeration is split across activities, so completeness can no longer be
guaranteed by "one activity ran to the end". It is guaranteed here instead: the
reducer refuses to compare anything against stored state until every page of the
list is present, self-consistent and duplicate-free.

That refusal is what preserves the date-boundary contract. A missing page would
otherwise silently reclassify every notice on it as "not seen this run", which is
indistinguishable from "unchanged" and would hide both retroactively registered
IDs and edits to old ones.

This module deliberately imports no Temporal symbols so the reducer contract is
testable without the SDK installed.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .backfill import build_backfill_manifest
from .models import NoticeListItem
from .pagination import PAGE_SIZE, expected_rows_for_page, total_pages_for

PAGE_RECEIPT_DIR = "discover_pages"


class DiscoveryReduceError(RuntimeError):
    """Raised when the page set cannot be proven complete and consistent."""


@dataclass(frozen=True, slots=True)
class PageReceipt:
    page: int
    total_count: int
    row_count: int
    items: tuple[NoticeListItem, ...]
    page_sha256: str

    def to_payload(self) -> dict[str, object]:
        return {
            "page": self.page,
            "total_count": self.total_count,
            "row_count": self.row_count,
            "page_sha256": self.page_sha256,
            "items": [
                {
                    "source_notice_id": item.source_notice_id,
                    "title": item.title,
                    "notice_date": item.notice_date.isoformat(),
                    "source_url": item.source_url,
                    "listing_fingerprint": item.listing_fingerprint,
                }
                for item in self.items
            ],
        }

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> PageReceipt:
        raw_items = payload["items"]
        assert isinstance(raw_items, list)
        items = tuple(
            NoticeListItem(
                source_notice_id=str(item["source_notice_id"]),
                title=str(item["title"]),
                notice_date=date.fromisoformat(str(item["notice_date"])),
                source_url=str(item["source_url"]),
                listing_fingerprint=str(item["listing_fingerprint"]),
            )
            for item in raw_items
        )
        return cls(
            page=int(payload["page"]),  # type: ignore[arg-type]
            total_count=int(payload["total_count"]),  # type: ignore[arg-type]
            row_count=int(payload["row_count"]),  # type: ignore[arg-type]
            items=items,
            page_sha256=str(payload["page_sha256"]),
        )


@dataclass(frozen=True, slots=True)
class ReducedIndex:
    items: tuple[NoticeListItem, ...]
    total_count: int
    total_pages: int
    page_count: int
    manifest_sha256: str


def page_digest(items: Sequence[NoticeListItem]) -> str:
    """Content digest of one page, independent of page position."""

    rows = [
        (
            item.source_notice_id,
            item.listing_fingerprint,
            item.notice_date.isoformat(),
        )
        for item in items
    ]
    return hashlib.sha256(
        json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def build_page_receipt(
    *,
    page: int,
    total_count: int,
    items: Sequence[NoticeListItem],
) -> PageReceipt:
    return PageReceipt(
        page=page,
        total_count=total_count,
        row_count=len(items),
        items=tuple(items),
        page_sha256=page_digest(items),
    )


def page_receipt_dir(root: Path) -> Path:
    path = root / PAGE_RECEIPT_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def page_receipt_path(root: Path, page: int) -> Path:
    return page_receipt_dir(root) / f"page-{page:04d}.json"


def write_page_receipt(root: Path, receipt: PageReceipt) -> Path:
    from .receipts import write_json

    path = page_receipt_path(root, receipt.page)
    write_json(path, receipt.to_payload())
    return path


def read_page_receipt(root: Path, page: int) -> PageReceipt | None:
    """Return a re-usable page receipt, or None when the page must be fetched.

    A receipt that cannot be parsed is treated as absent so a retry re-fetches
    the page rather than reducing over corrupt input.
    """

    path = page_receipt_path(root, page)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        receipt = PageReceipt.from_payload(payload)
    except (ValueError, KeyError, TypeError):
        return None
    if receipt.page != page or receipt.page_sha256 != page_digest(receipt.items):
        return None
    return receipt


def load_page_receipts(root: Path) -> tuple[PageReceipt, ...]:
    directory = page_receipt_dir(root)
    receipts: list[PageReceipt] = []
    for path in sorted(directory.glob("page-*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            receipts.append(PageReceipt.from_payload(payload))
        except (ValueError, KeyError, TypeError) as error:
            raise DiscoveryReduceError(
                f"page receipt is unreadable: {path.name} ({type(error).__name__})"
            ) from error
    return tuple(receipts)


def reduce_page_receipts(receipts: Iterable[PageReceipt]) -> ReducedIndex:
    """Fail closed unless the page set proves a complete, unique enumeration.

    Every check below must hold before the caller is allowed to compare against
    stored state. A partial page set raises; it never yields a partial index.
    """

    ordered = sorted(receipts, key=lambda receipt: receipt.page)
    if not ordered:
        raise DiscoveryReduceError("no page receipts were produced")

    pages = [receipt.page for receipt in ordered]
    duplicate_pages = sorted({page for page in pages if pages.count(page) > 1})
    if duplicate_pages:
        raise DiscoveryReduceError(f"duplicate page receipts: {duplicate_pages}")

    totals = {receipt.total_count for receipt in ordered}
    if len(totals) != 1:
        raise DiscoveryReduceError(
            "page receipts disagree on total_count: " + repr(sorted(totals))
        )
    total_count = totals.pop()
    if total_count < 1:
        raise DiscoveryReduceError(f"index total_count is not positive: {total_count}")
    total_pages = total_pages_for(total_count)

    missing = sorted(set(range(1, total_pages + 1)).difference(pages))
    if missing:
        raise DiscoveryReduceError(
            f"incomplete page set: missing={missing} "
            f"have={len(pages)} expect={total_pages}"
        )
    unexpected = sorted(page for page in pages if page > total_pages or page < 1)
    if unexpected:
        raise DiscoveryReduceError(f"page receipts outside the index: {unexpected}")

    for receipt in ordered:
        expected_rows = expected_rows_for_page(
            receipt.page,
            total_count=total_count,
            total_pages=total_pages,
        )
        if receipt.row_count != expected_rows or len(receipt.items) != expected_rows:
            raise DiscoveryReduceError(
                f"page row gap: page={receipt.page} rows={len(receipt.items)} "
                f"row_count={receipt.row_count} expected={expected_rows}"
            )
        if receipt.page_sha256 != page_digest(receipt.items):
            raise DiscoveryReduceError(
                f"page digest mismatch: page={receipt.page}"
            )

    items = tuple(item for receipt in ordered for item in receipt.items)
    if len(items) != total_count:
        raise DiscoveryReduceError(
            f"assembled item gap: items={len(items)} total={total_count}"
        )
    seen: set[str] = set()
    duplicate_ids: list[str] = []
    for item in items:
        if item.source_notice_id in seen:
            duplicate_ids.append(item.source_notice_id)
        seen.add(item.source_notice_id)
    if duplicate_ids:
        raise DiscoveryReduceError(
            f"duplicate notice ids across pages: {sorted(set(duplicate_ids))}"
        )

    return ReducedIndex(
        items=items,
        total_count=total_count,
        total_pages=total_pages,
        page_count=len(ordered),
        manifest_sha256=build_backfill_manifest(
            items,
            chunk_size=max(1, total_count),
        ).manifest_sha256,
    )


__all__ = [
    "PAGE_RECEIPT_DIR",
    "PAGE_SIZE",
    "DiscoveryReduceError",
    "PageReceipt",
    "ReducedIndex",
    "build_page_receipt",
    "load_page_receipts",
    "page_digest",
    "page_receipt_dir",
    "page_receipt_path",
    "read_page_receipt",
    "reduce_page_receipts",
    "write_page_receipt",
]
