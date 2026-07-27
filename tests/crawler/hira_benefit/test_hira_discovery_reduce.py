"""Fail-closed contract for the split discovery reducer.

The split moved completeness from "one activity ran to the end" to "the reducer
proved the page set is whole". These tests are that proof, including the two
retroactive-detection capabilities the split must not cost us.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from pipeline.scripts.crawler.hira_benefit.change_detection import (
    StoredNoticeState,
    plan_changes,
)
from pipeline.scripts.crawler.hira_benefit.discovery import (
    DiscoveryReduceError,
    PageReceipt,
    build_page_receipt,
    load_page_receipts,
    page_digest,
    page_receipt_path,
    read_page_receipt,
    reduce_page_receipts,
    write_page_receipt,
)
from pipeline.scripts.crawler.hira_benefit.models import NoticeListItem
from pipeline.scripts.crawler.hira_benefit.pagination import PAGE_SIZE

TOTAL = 61  # 3 pages: 30 + 30 + 1


def _item(index: int, *, title: str | None = None) -> NoticeListItem:
    return NoticeListItem.create(
        source_notice_id=str(index),
        title=title or f"notice {index}",
        notice_date=date(2026, 7, 25),
        source_url=f"https://www.hira.or.kr/detail?brdBltNo={index}",
    )


def _pages(total: int = TOTAL) -> list[PageReceipt]:
    ids = list(range(total, 0, -1))
    receipts: list[PageReceipt] = []
    for page_index, start in enumerate(range(0, total, PAGE_SIZE), start=1):
        receipts.append(
            build_page_receipt(
                page=page_index,
                total_count=total,
                items=[_item(value) for value in ids[start : start + PAGE_SIZE]],
            )
        )
    return receipts


def _stored(receipts: list[PageReceipt]) -> dict[str, StoredNoticeState]:
    return {
        item.source_notice_id: StoredNoticeState(
            source_notice_id=item.source_notice_id,
            listing_fingerprint=item.listing_fingerprint,
        )
        for receipt in receipts
        for item in receipt.items
    }


def test_complete_page_set_reduces_to_a_unique_gap_free_index() -> None:
    reduced = reduce_page_receipts(_pages())

    assert reduced.total_count == TOTAL
    assert reduced.total_pages == 3
    assert reduced.page_count == 3
    assert len(reduced.items) == TOTAL
    assert len({item.source_notice_id for item in reduced.items}) == TOTAL
    assert len(reduced.manifest_sha256) == 64


# --- fault injection (1): a missing page must stop the comparison -------------


def test_missing_page_receipt_fails_closed_and_never_compares() -> None:
    pages = _pages()
    del pages[1]

    with pytest.raises(DiscoveryReduceError, match=r"incomplete page set.*missing=\[2\]"):
        reduce_page_receipts(pages)


def test_missing_last_page_fails_closed() -> None:
    pages = _pages()
    del pages[-1]

    with pytest.raises(DiscoveryReduceError, match="incomplete page set"):
        reduce_page_receipts(pages)


def test_empty_page_set_fails_closed() -> None:
    with pytest.raises(DiscoveryReduceError, match="no page receipts"):
        reduce_page_receipts([])


# --- fault injection (2): total count disagreement ----------------------------


def test_total_count_disagreement_between_pages_fails_closed() -> None:
    pages = _pages()
    pages[1] = PageReceipt(
        page=pages[1].page,
        total_count=TOTAL + 1,
        row_count=pages[1].row_count,
        items=pages[1].items,
        page_sha256=pages[1].page_sha256,
    )

    with pytest.raises(DiscoveryReduceError, match="disagree on total_count"):
        reduce_page_receipts(pages)


def test_row_count_gap_within_a_page_fails_closed() -> None:
    pages = _pages()
    trimmed = pages[0].items[:-1]
    pages[0] = build_page_receipt(page=1, total_count=TOTAL, items=trimmed)

    with pytest.raises(DiscoveryReduceError, match="page row gap"):
        reduce_page_receipts(pages)


# --- fault injection (3): duplicate identities --------------------------------


def test_duplicate_notice_id_across_pages_fails_closed() -> None:
    pages = _pages()
    collided = (_item(61), *pages[1].items[1:])
    pages[1] = build_page_receipt(page=2, total_count=TOTAL, items=collided)

    with pytest.raises(DiscoveryReduceError, match="duplicate notice ids"):
        reduce_page_receipts(pages)


def test_duplicate_page_receipt_fails_closed() -> None:
    pages = _pages()
    pages.append(pages[0])

    with pytest.raises(DiscoveryReduceError, match="duplicate page receipts"):
        reduce_page_receipts(pages)


def test_tampered_page_digest_fails_closed() -> None:
    pages = _pages()
    pages[0] = PageReceipt(
        page=1,
        total_count=TOTAL,
        row_count=pages[0].row_count,
        items=pages[0].items,
        page_sha256="0" * 64,
    )

    with pytest.raises(DiscoveryReduceError, match="page digest mismatch"):
        reduce_page_receipts(pages)


# --- fault injection (4): retroactively registered notice ---------------------


def test_retroactively_inserted_notice_is_still_detected_as_new() -> None:
    """A back-dated ID appearing mid-list must survive the split as ``new``.

    This is the capability the date-boundary contract exists for: HIRA can
    register a notice with an old date after we last looked.
    """

    baseline = _pages()
    stored = _stored(baseline)

    retro = _item(9001, title="retroactively registered notice")
    grown = list(baseline[0].items) + [retro]
    pages = [
        build_page_receipt(page=1, total_count=TOTAL + 1, items=grown[:PAGE_SIZE]),
        build_page_receipt(
            page=2,
            total_count=TOTAL + 1,
            items=[grown[PAGE_SIZE], *baseline[1].items[:-1]],
        ),
        build_page_receipt(
            page=3,
            total_count=TOTAL + 1,
            items=[baseline[1].items[-1], *baseline[2].items],
        ),
    ]

    reduced = reduce_page_receipts(pages)
    plan = plan_changes(reduced.items, stored=stored)

    assert reduced.total_count == TOTAL + 1
    assert [item.source_notice_id for item in plan.new] == ["9001"]
    assert plan.changed == ()
    assert len(plan.unchanged) == TOTAL
    assert retro in plan.to_fetch


# --- fault injection (5): edited historical notice ----------------------------


def test_edited_historical_notice_is_still_detected_as_changed() -> None:
    """An edit to an old ID must survive the split as ``changed``."""

    baseline = _pages()
    stored = _stored(baseline)

    edited = _item(1, title="notice 1 (개정)")
    pages = list(baseline)
    pages[2] = build_page_receipt(page=3, total_count=TOTAL, items=[edited])

    reduced = reduce_page_receipts(pages)
    plan = plan_changes(reduced.items, stored=stored)

    assert [item.source_notice_id for item in plan.changed] == ["1"]
    assert plan.new == ()
    assert len(plan.unchanged) == TOTAL - 1
    assert edited in plan.to_fetch


def test_unchanged_index_produces_no_work() -> None:
    baseline = _pages()

    plan = plan_changes(
        reduce_page_receipts(baseline).items,
        stored=_stored(baseline),
    )

    assert plan.new == ()
    assert plan.changed == ()
    assert plan.to_fetch == ()


# --- durable page receipts ----------------------------------------------------


def test_page_receipt_round_trips_and_is_reusable(tmp_path: Path) -> None:
    receipt = _pages()[0]

    write_page_receipt(tmp_path, receipt)
    restored = read_page_receipt(tmp_path, 1)

    assert restored == receipt
    assert restored is not None
    assert restored.page_sha256 == page_digest(receipt.items)


def test_corrupt_page_receipt_is_treated_as_absent_so_a_retry_refetches(
    tmp_path: Path,
) -> None:
    receipt = _pages()[0]
    write_page_receipt(tmp_path, receipt)
    page_receipt_path(tmp_path, 1).write_text("{not json", encoding="utf-8")

    assert read_page_receipt(tmp_path, 1) is None


def test_reducer_rejects_an_unreadable_receipt_rather_than_skipping_it(
    tmp_path: Path,
) -> None:
    for receipt in _pages():
        write_page_receipt(tmp_path, receipt)
    page_receipt_path(tmp_path, 2).write_text("{not json", encoding="utf-8")

    with pytest.raises(DiscoveryReduceError, match="unreadable"):
        load_page_receipts(tmp_path)


def test_page_receipts_load_in_page_order_regardless_of_write_order(
    tmp_path: Path,
) -> None:
    for receipt in reversed(_pages()):
        write_page_receipt(tmp_path, receipt)

    loaded = load_page_receipts(tmp_path)

    assert [receipt.page for receipt in loaded] == [1, 2, 3]
