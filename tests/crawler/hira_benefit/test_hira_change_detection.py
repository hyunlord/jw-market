from __future__ import annotations

from datetime import date

import pytest

from pipeline.scripts.crawler.hira_benefit.change_detection import (
    FirstRunMode,
    StoredNoticeState,
    plan_changes,
)
from pipeline.scripts.crawler.hira_benefit.models import NoticeListItem


def _item(notice_id: str, title: str, day: int) -> NoticeListItem:
    return NoticeListItem.create(
        source_notice_id=notice_id,
        title=title,
        notice_date=date(2026, 7, day),
        source_url=f"https://www.hira.or.kr/detail?brdBltNo={notice_id}",
    )


def test_first_run_requires_explicit_policy() -> None:
    with pytest.raises(ValueError, match="first_run_mode"):
        plan_changes([_item("100", "first", 1)], stored=None)


def test_recent_first_run_is_bounded_and_deterministic() -> None:
    items = [_item("100", "old", 1), _item("102", "new", 3), _item("101", "middle", 2)]

    plan = plan_changes(
        items,
        stored=None,
        first_run_mode=FirstRunMode.RECENT_N,
        recent_limit=2,
    )

    assert [item.source_notice_id for item in plan.to_fetch] == ["102", "101"]
    assert plan.skipped_initial_backfill == 1


def test_change_detection_uses_stable_fingerprint_not_numeric_watermark() -> None:
    original = _item("102", "notice title", 3)
    edited = _item("102", "notice title corrected", 3)
    lower_new_id = _item("099", "late registration", 4)
    stored = {
        "102": StoredNoticeState(
            source_notice_id="102",
            listing_fingerprint=original.listing_fingerprint,
        )
    }

    plan = plan_changes([edited, lower_new_id], stored=stored)

    assert [item.source_notice_id for item in plan.changed] == ["102"]
    assert [item.source_notice_id for item in plan.new] == ["099"]
    assert {item.source_notice_id for item in plan.to_fetch} == {"102", "099"}


def test_unchanged_items_are_not_fetched_again() -> None:
    item = _item("102", "same", 3)
    plan = plan_changes(
        [item],
        stored={
            "102": StoredNoticeState(
                source_notice_id="102",
                listing_fingerprint=item.listing_fingerprint,
            )
        },
    )

    assert plan.to_fetch == ()
    assert plan.unchanged == (item,)
