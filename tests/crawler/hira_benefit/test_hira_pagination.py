from __future__ import annotations

from datetime import date
from pipeline.scripts.crawler.hira_benefit.pagination import fetch_notice_index


def _page_html(*, total: int, page: int, rows: int) -> str:
    start = total - ((page - 1) * 30)
    body = [
        f'<div>전체 : <span class="fcO">{total:,}</span>건</div>',
    ]
    for offset in range(rows):
        notice_id = str(start - offset)
        body.append(
            "<tr>"
            f'<td><a href="/rc/drug/insuadtcrtr/bbsView.do?brdBltNo={notice_id}">'
            f"notice {notice_id}</a></td>"
            '<td class="col-date">2026-07-25</td>'
            "</tr>"
        )
    return "".join(body)


def test_pagination_fetches_every_page_and_returns_stable_unique_manifest() -> None:
    requested_pages: list[int] = []

    def fetch_form(_url: str, form: dict[str, str]) -> str:
        page = int(form["pageIndex"])
        requested_pages.append(page)
        rows = 1 if page == 3 else 30
        return _page_html(total=61, page=page, rows=rows)

    result = fetch_notice_index(
        index_url=(
            "https://www.hira.or.kr/rc/insu/insuadtcrtr/"
            "InsuAdtCrtrList.do?pgmid=HIRAA030069000400"
        ),
        base_url="https://www.hira.or.kr",
        fetch_form=fetch_form,
    )

    assert requested_pages == [1, 2, 3]
    assert result.total_count == 61
    assert result.total_pages == 3
    assert len(result.items) == 61
    assert result.items[0].notice_date == date(2026, 7, 25)
    assert len({item.source_notice_id for item in result.items}) == 61
    assert len(result.manifest_sha256) == 64
