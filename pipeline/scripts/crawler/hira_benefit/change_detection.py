from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from enum import Enum

from .models import NoticeListItem


class FirstRunMode(str, Enum):
    BACKFILL_ALL = "backfill_all"
    DATE_BOUNDARY = "date_boundary"


@dataclass(frozen=True, slots=True)
class StoredNoticeState:
    source_notice_id: str
    listing_fingerprint: str


@dataclass(frozen=True, slots=True)
class ChangePlan:
    new: tuple[NoticeListItem, ...]
    changed: tuple[NoticeListItem, ...]
    unchanged: tuple[NoticeListItem, ...]
    to_fetch: tuple[NoticeListItem, ...]
    skipped_initial_backfill: int = 0


def _newest_first(item: NoticeListItem) -> tuple[object, int | str]:
    notice_id: int | str
    try:
        notice_id = int(item.source_notice_id)
    except ValueError:
        notice_id = item.source_notice_id
    return item.notice_date, notice_id


def plan_changes(
    items: Sequence[NoticeListItem],
    *,
    stored: Mapping[str, StoredNoticeState] | None,
    first_run_mode: FirstRunMode | str | None = None,
    notice_date_boundary: date | str | None = None,
) -> ChangePlan:
    """Plan detail fetches without treating brdBltNo as a monotonic watermark."""

    ordered = tuple(sorted(items, key=_newest_first, reverse=True))
    if stored is None:
        if first_run_mode is None:
            raise ValueError("first_run_mode is required when no crawl state exists")
        mode = FirstRunMode(first_run_mode)
        if mode is FirstRunMode.DATE_BOUNDARY:
            if notice_date_boundary is None:
                raise ValueError(
                    "notice_date_boundary is required for date_boundary"
                )
            boundary = (
                date.fromisoformat(notice_date_boundary)
                if isinstance(notice_date_boundary, str)
                else notice_date_boundary
            )
            selected = tuple(
                item for item in ordered if item.notice_date >= boundary
            )
            skipped = max(0, len(ordered) - len(selected))
        else:
            selected = ordered
            skipped = 0
        return ChangePlan(
            new=selected,
            changed=(),
            unchanged=(),
            to_fetch=selected,
            skipped_initial_backfill=skipped,
        )

    new: list[NoticeListItem] = []
    changed: list[NoticeListItem] = []
    unchanged: list[NoticeListItem] = []
    for item in ordered:
        previous = stored.get(item.source_notice_id)
        if previous is None:
            new.append(item)
        elif previous.listing_fingerprint != item.listing_fingerprint:
            changed.append(item)
        else:
            unchanged.append(item)
    return ChangePlan(
        new=tuple(new),
        changed=tuple(changed),
        unchanged=tuple(unchanged),
        to_fetch=(*new, *changed),
    )
