from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

from .models import NoticeListItem


class FirstRunMode(str, Enum):
    BACKFILL_ALL = "backfill_all"
    RECENT_N = "recent_n"


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
    recent_limit: int | None = None,
) -> ChangePlan:
    """Plan detail fetches without treating brdBltNo as a monotonic watermark."""

    ordered = tuple(sorted(items, key=_newest_first, reverse=True))
    if stored is None:
        if first_run_mode is None:
            raise ValueError("first_run_mode is required when no crawl state exists")
        mode = FirstRunMode(first_run_mode)
        if mode is FirstRunMode.RECENT_N:
            if recent_limit is None or recent_limit <= 0:
                raise ValueError("recent_limit must be positive for recent_n")
            selected = ordered[:recent_limit]
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
