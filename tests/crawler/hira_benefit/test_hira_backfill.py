from __future__ import annotations

from datetime import date

from pipeline.scripts.crawler.hira_benefit.backfill import (
    BackfillManifest,
    build_backfill_manifest,
    compare_manifest_state,
    next_pending_chunk,
)
from pipeline.scripts.crawler.hira_benefit.backfill_cli import _parser
from pipeline.scripts.crawler.hira_benefit.change_detection import StoredNoticeState
from pipeline.scripts.crawler.hira_benefit.contract import HiraWorkflowInput
from pipeline.scripts.crawler.hira_benefit.models import NoticeListItem


def _items(count: int) -> tuple[NoticeListItem, ...]:
    return tuple(
        NoticeListItem.create(
            source_notice_id=str(10_000 - index),
            title=f"notice {index}",
            notice_date=date(2026, 7, 25),
            source_url=(
                "https://www.hira.or.kr/rc/drug/insuadtcrtr/"
                f"bbsView.do?brdBltNo={10_000 - index}"
            ),
        )
        for index in range(count)
    )


def test_manifest_splits_4577_notices_into_ten_approved_chunks() -> None:
    manifest = build_backfill_manifest(_items(4_577), chunk_size=500)

    assert manifest.total_count == 4_577
    assert manifest.chunk_count == 10
    assert [len(chunk.items) for chunk in manifest.chunks] == [500] * 9 + [77]
    assert len(manifest.manifest_sha256) == 64


def test_prepare_defaults_to_the_live_hira_page_identity() -> None:
    args = _parser().parse_args(
        ["prepare", "--manifest", "/tmp/hira-manifest.json"]
    )
    workflow = HiraWorkflowInput(
        run_id="scheduled",
        state_root="/tmp/hira-state",
        first_run_mode="date_boundary",
        notice_date_boundary="2023-12-29",
        expected_detail_notices=120,
    )

    assert args.index_url.endswith(
        "InsuAdtCrtrList.do?pgmid=HIRAA030069000400"
    )
    assert args.index_url == workflow.index_url


def test_next_pending_chunk_resumes_after_last_complete_receipt() -> None:
    manifest = build_backfill_manifest(_items(1_077), chunk_size=500)

    pending = next_pending_chunk(
        manifest,
        completed_chunk_indexes={0, 1},
    )

    assert pending is not None
    assert pending.index == 2
    assert len(pending.items) == 77


def test_manifest_round_trip_preserves_identity_and_hash() -> None:
    manifest = build_backfill_manifest(_items(31), chunk_size=10)

    restored = BackfillManifest.from_json(manifest.to_json())

    assert restored == manifest


def test_final_manifest_gate_reports_missing_and_hash_mismatch() -> None:
    manifest = build_backfill_manifest(_items(3), chunk_size=2)
    stored = {
        manifest.chunks[0].items[0].source_notice_id: StoredNoticeState(
            source_notice_id=manifest.chunks[0].items[0].source_notice_id,
            listing_fingerprint=manifest.chunks[0].items[0].listing_fingerprint,
        ),
        manifest.chunks[0].items[1].source_notice_id: StoredNoticeState(
            source_notice_id=manifest.chunks[0].items[1].source_notice_id,
            listing_fingerprint="mismatch",
        ),
    }

    gate = compare_manifest_state(manifest, stored)

    assert gate.expected_count == 3
    assert gate.matched_count == 1
    assert gate.missing_ids == (manifest.chunks[1].items[0].source_notice_id,)
    assert gate.hash_mismatch_ids == (
        manifest.chunks[0].items[1].source_notice_id,
    )
    assert gate.passed is False


def test_final_manifest_gate_passes_only_for_all_4577_exact_identities() -> None:
    manifest = build_backfill_manifest(_items(4_577), chunk_size=500)
    stored = {
        item.source_notice_id: StoredNoticeState(
            source_notice_id=item.source_notice_id,
            listing_fingerprint=item.listing_fingerprint,
        )
        for chunk in manifest.chunks
        for item in chunk.items
    }

    gate = compare_manifest_state(manifest, stored)

    assert gate.passed is True
    assert gate.expected_count == 4_577
    assert gate.matched_count == 4_577
    assert gate.missing_ids == ()
    assert gate.hash_mismatch_ids == ()
