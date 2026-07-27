"""Stage-level contract for split enumeration.

Every fetch here goes through an in-process fake: this suite makes zero live
HIRA requests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.scripts.crawler.hira_benefit import stage_cli
from pipeline.scripts.crawler.hira_benefit.contract import (
    HiraWorkflowInput,
    page_batches,
)
from pipeline.scripts.crawler.hira_benefit.discovery import (
    load_page_receipts,
    read_page_receipt,
    reduce_page_receipts,
)
from pipeline.scripts.crawler.hira_benefit.pagination import PAGE_SIZE, fetch_page

TOTAL = 121  # 5 pages: 30 x 4 + 1


def _page_html(*, total: int, page: int) -> str:
    rows = total - PAGE_SIZE * (page - 1)
    rows = PAGE_SIZE if rows > PAGE_SIZE else rows
    start = total - ((page - 1) * PAGE_SIZE)
    body = [f'<div>전체 : <span class="fcO">{total:,}</span>건</div>']
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


class FakeClient:
    """Counts every request so "minimum HIRA traffic" is an assertion, not a hope."""

    def __init__(self, *, total: int = TOTAL, fail_pages: set[int] | None = None) -> None:
        self.total = total
        self.fail_pages = fail_pages or set()
        self.requested: list[int] = []

    def post_form_text(self, _url: str, form: dict[str, str]) -> str:
        page = int(form["pageIndex"])
        self.requested.append(page)
        if page in self.fail_pages:
            raise RuntimeError(f"injected transport failure on page {page}")
        return _page_html(total=self.total, page=page)


def _config(tmp_path: Path, **overrides: object) -> HiraWorkflowInput:
    base: dict[str, object] = {
        "run_id": "run-split",
        "state_root": str(tmp_path),
        "first_run_mode": "date_boundary",
        "notice_date_boundary": "2023-12-29",
        "expected_detail_notices": 120,
    }
    base.update(overrides)
    return HiraWorkflowInput(**base)  # type: ignore[arg-type]


@pytest.fixture
def patched_client(monkeypatch: pytest.MonkeyPatch) -> FakeClient:
    client = FakeClient()
    monkeypatch.setattr(stage_cli, "_client", lambda *_a, **_k: client)
    return client


def test_probe_fetches_only_page_one_and_reports_the_batch_plan(
    tmp_path: Path,
    patched_client: FakeClient,
) -> None:
    config = _config(tmp_path)
    root = tmp_path / "run"
    root.mkdir()

    receipt = stage_cli._run_discover_probe(config, root)

    assert patched_client.requested == [1]
    assert receipt["status"] == "complete"
    assert receipt["total_count"] == TOTAL
    assert receipt["total_pages"] == 5
    assert read_page_receipt(root, 1) is not None


def test_page_batch_fetches_only_its_slice(
    tmp_path: Path,
    patched_client: FakeClient,
) -> None:
    config = _config(tmp_path)
    root = tmp_path / "run"
    root.mkdir()

    receipt = stage_cli._run_discover_page_batch(config, root, page_start=2, page_end=4)

    assert patched_client.requested == [2, 3, 4]
    assert receipt["pages_fetched"] == 3
    assert receipt["pages_cached"] == 0
    assert sorted(r.page for r in load_page_receipts(root)) == [2, 3, 4]


def test_retrying_a_batch_reuses_page_receipts_instead_of_refetching(
    tmp_path: Path,
    patched_client: FakeClient,
) -> None:
    """Idempotence: only the pages a failed attempt never reached are re-fetched."""

    config = _config(tmp_path)
    root = tmp_path / "run"
    root.mkdir()

    patched_client.fail_pages = {4}
    with pytest.raises(RuntimeError, match="injected transport failure"):
        stage_cli._run_discover_page_batch(config, root, page_start=2, page_end=5)
    assert patched_client.requested == [2, 3, 4]

    patched_client.fail_pages = set()
    patched_client.requested.clear()
    receipt = stage_cli._run_discover_page_batch(config, root, page_start=2, page_end=5)

    # Pages 2 and 3 already landed; the retry only pays for 4 and 5.
    assert patched_client.requested == [4, 5]
    assert receipt["pages_cached"] == 2
    assert receipt["pages_fetched"] == 2


def test_batch_larger_than_the_budget_is_refused_before_any_request(
    tmp_path: Path,
    patched_client: FakeClient,
) -> None:
    config = _config(tmp_path)
    root = tmp_path / "run"
    root.mkdir()

    with pytest.raises(RuntimeError, match="exceeds the budgeted"):
        stage_cli._run_discover_page_batch(config, root, page_start=2, page_end=40)

    assert patched_client.requested == []


def test_page_beyond_the_index_fails_closed(tmp_path: Path) -> None:
    client = FakeClient()

    with pytest.raises(RuntimeError, match="page out of range"):
        fetch_page(
            9,
            index_url="https://www.hira.or.kr/rc/insu/insuadtcrtr/InsuAdtCrtrList.do",
            base_url="https://www.hira.or.kr",
            fetch_form=client.post_form_text,
        )


def test_probe_and_batches_together_cover_every_page_exactly_once(
    tmp_path: Path,
    patched_client: FakeClient,
) -> None:
    """The split must enumerate the same set the single activity used to."""

    config = _config(tmp_path)
    root = tmp_path / "run"
    root.mkdir()

    probe = stage_cli._run_discover_probe(config, root)
    total_pages = int(probe["total_pages"])  # type: ignore[arg-type]
    for start, end in page_batches(total_pages, config.pages_per_batch):
        stage_cli._run_discover_page_batch(config, root, page_start=start, page_end=end)

    assert sorted(patched_client.requested) == [1, 2, 3, 4, 5]
    assert len(patched_client.requested) == len(set(patched_client.requested))

    reduced = reduce_page_receipts(load_page_receipts(root))

    assert reduced.total_count == TOTAL
    assert len(reduced.items) == TOTAL
    assert len({item.source_notice_id for item in reduced.items}) == TOTAL


def test_probe_and_batch_do_not_apply_to_a_backfill_chunk(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        first_run_mode="backfill_all",
        notice_date_boundary=None,
        manifest_path="/tmp/manifest.json",
        manifest_sha256="a" * 64,
        chunk_index=0,
    )
    root = tmp_path / "run"
    root.mkdir()

    with pytest.raises(RuntimeError, match="does not apply to a backfill chunk"):
        stage_cli._run_discover_probe(config, root)
    with pytest.raises(RuntimeError, match="does not apply to a backfill chunk"):
        stage_cli._run_discover_page_batch(config, root, page_start=2, page_end=3)


def test_reduce_failure_receipt_is_a_non_retryable_gate_failure() -> None:
    from pipeline.scripts.crawler.hira_benefit.discovery import DiscoveryReduceError

    receipt = stage_cli.build_failure_receipt(
        "discover_reduce",
        DiscoveryReduceError("incomplete page set: missing=[7]"),
    )

    assert receipt["status"] == "failed"
    assert receipt["gate_failures"] == ["discovery_incomplete"]


def test_stage_cli_exposes_the_split_stages_and_no_monolithic_discover() -> None:
    assert set(stage_cli._RUNNERS) == {
        "discover_probe",
        "discover_page_batch",
        "discover_reduce",
        "collect_details",
        "persist_results",
        "verify_run",
    }
    assert "discover_changes" not in stage_cli._RUNNERS
