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


def test_date_boundary_includes_the_entire_boundary_day_bundle() -> None:
    boundary_bundle = tuple(
        NoticeListItem.create(
            source_notice_id=str(1_000 + index),
            title=f"boundary notice {index}",
            notice_date=date(2023, 12, 29),
            source_url=f"https://www.hira.or.kr/detail?brdBltNo={1_000 + index}",
        )
        for index in range(68)
    )
    newer = NoticeListItem.create(
        source_notice_id="2000",
        title="newer",
        notice_date=date(2024, 1, 2),
        source_url="https://www.hira.or.kr/detail?brdBltNo=2000",
    )
    older = NoticeListItem.create(
        source_notice_id="999",
        title="older",
        notice_date=date(2023, 12, 28),
        source_url="https://www.hira.or.kr/detail?brdBltNo=999",
    )

    plan = plan_changes(
        (*boundary_bundle, newer, older),
        stored=None,
        first_run_mode=FirstRunMode.DATE_BOUNDARY,
        notice_date_boundary=date(2023, 12, 29),
    )

    assert len(plan.to_fetch) == 69
    assert sum(item.notice_date == date(2023, 12, 29) for item in plan.to_fetch) == 68
    assert all(item.notice_date >= date(2023, 12, 29) for item in plan.to_fetch)
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
