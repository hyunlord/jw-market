from __future__ import annotations

from datetime import date

import pytest

from pipeline.scripts.crawler.hira_benefit.contract import HiraWorkflowInput
from pipeline.scripts.crawler.hira_benefit.models import NoticeListItem
from pipeline.scripts.crawler.hira_benefit.service import (
    collect_details,
    discover_changes,
    plan_discovered_items,
    tag_sequence_signature,
)
from pipeline.scripts.crawler.hira_benefit.scope import BrandScopeEntry


def test_tag_signature_ignores_volatile_text_and_attributes() -> None:
    first = '<div data-session="one"><span>조회수 1</span></div>'
    second = '<div data-session="two"><span>조회수 99</span></div>'

    assert tag_sequence_signature(first) == tag_sequence_signature(second)


def test_zero_row_index_fails_closed() -> None:
    config = HiraWorkflowInput(
        run_id="run",
        state_root="/tmp/state",
        first_run_mode="date_boundary",
        notice_date_boundary="2026-07-01",
    )

    with pytest.raises(RuntimeError, match="zero notices"):
        discover_changes("<html><body>페이지 정보가 존재하지 않습니다.</body></html>", config=config, stored=None)


def test_collect_details_keeps_failed_parse_as_raw_fallback() -> None:
    item = NoticeListItem.create(
        source_notice_id="1",
        title="notice",
        notice_date=date(2026, 7, 25),
        source_url="https://www.hira.or.kr/detail?brdBltNo=1",
    )

    rows, metrics = collect_details(
        (item,),
        fetch_text=lambda _url: "<p>리바로 관련 첨부파일을 확인하십시오.</p>",
        brands=(BrandScopeEntry("리바로", "리바로", ("C10A1",)),),
        molecules=(),
    )

    assert len(rows) == 1
    assert rows[0].parsed.parse_status.value == "FAILED"
    assert rows[0].brand_names == ("리바로",)
    assert metrics.failures == 0
    assert metrics.failed_count == 1


def test_collect_details_derives_dosage_suffixes_from_current_batch() -> None:
    items = tuple(
        NoticeListItem.create(
            source_notice_id=str(index),
            title=f"notice-{index}",
            notice_date=date(2026, 7, 25),
            source_url=f"https://www.hira.or.kr/detail?brdBltNo={index}",
        )
        for index in range(1, 4)
    )
    brands = tuple(
        BrandScopeEntry(name, name, ("A01A0",))
        for name in ("첫째", "둘째", "셋째")
    )
    html_by_url = {
        item.source_url: f"<p>품명: {name}정</p>"
        for item, name in zip(items, ("첫째", "둘째", "셋째"), strict=True)
    }

    rows, _metrics = collect_details(
        items,
        fetch_text=html_by_url.__getitem__,
        brands=brands,
        molecules=(),
    )

    assert [row.brand_names for row in rows] == [("첫째",), ("둘째",), ("셋째",)]


def test_full_population_plan_is_not_rejected_by_old_500_row_limit() -> None:
    items = tuple(
        NoticeListItem.create(
            source_notice_id=str(index),
            title=f"notice {index}",
            notice_date=date(2026, 7, 25),
            source_url=f"https://www.hira.or.kr/detail?brdBltNo={index}",
        )
        for index in range(501)
    )
    config = HiraWorkflowInput(
        run_id="run",
        state_root="/tmp/state",
        first_run_mode="backfill_all",
    )

    plan = plan_discovered_items(items, config=config, stored=None)

    assert len(plan.to_fetch) == 501
