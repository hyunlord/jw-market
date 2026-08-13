"""G4 — G3 workbook (.xlsx) structural validation for UBIST wide submissions.

The real UBIST submission is a wide workbook (2-row header, month columns) loaded
via --stage s1, where the s2 catalog gate never runs. Before G4, G3 pinned only
the sha256 and let a valid-sha but structurally-broken workbook through. These
tests exercise the reject classes and the pass path, all through the loader's own
parser (ubist_loader) so G3 and the loader share one contract.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import openpyxl
import pytest

from pipeline.scripts.ingest_hook.category_map import resolve_category
from pipeline.scripts.ingest_hook.contract import load_manifest
from pipeline.scripts.ingest_hook.g3 import G3Error, validate
from ingest_fixtures import write_ubist_workbook_submission

SPEC = resolve_category("ubist")


def _validate(bucket, manifest_path, **kwargs):
    return validate(load_manifest(manifest_path), SPEC, bucket, **kwargs)


def _write_workbook(path: Path, periods: tuple[str, ...]) -> dict[str, object]:
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.append(["브랜드", *(["처방조제액(원)"] * len(periods))])
    worksheet.append(["브랜드", *[f"{period[:4]}년 {int(period[5:])}월" for period in periods]])
    worksheet.append(["브랜드A", *([1.0] * len(periods))])
    workbook.save(path)
    return {
        "path": path.as_posix(),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "rows": 1,
        "period_start": "2021-01",
        "period_end": "2026-02",
    }


def _write_manifest(bucket: Path, *, epoch: str, files: list[dict[str, object]]) -> Path:
    for entry in files:
        entry["path"] = Path(str(entry["path"])).relative_to(bucket).as_posix()
    payload = {
        "contract_version": "v2",
        "epoch": epoch,
        "category": "ubist",
        "complete": True,
        "files": files,
    }
    path = bucket / "manifest.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_good_workbook_passes(bucket):
    manifest_path = write_ubist_workbook_submission(bucket, periods=("2026-06", "2026-07"))
    report = _validate(bucket, manifest_path)
    assert "2026-07" in report.observed_periods
    assert any("workbook validated via loader parser" in note for note in report.notes)


def test_collection_epoch_comes_from_internal_periods_not_names_or_declared_range(bucket):
    files = [
        _write_workbook(bucket / "병원 2021.xlsx", ("2021-01",)),
        _write_workbook(bucket / "Sales_2506.xlsx", ("2026-06",)),
    ]
    manifest_path = _write_manifest(bucket, epoch="2026-06", files=files)

    report = _validate(bucket, manifest_path)
    persisted_manifest = load_manifest(manifest_path)

    assert report.epoch == "2026-06"
    assert report.observed_periods == {"2021-01", "2026-06"}
    assert any("epoch_source_files" in note and "Sales_2506.xlsx" in note for note in report.notes)
    assert {(entry.period_start, entry.period_end) for entry in persisted_manifest.files} == {
        ("2021-01", "2026-02")
    }


def test_collection_allows_historical_workbooks_without_target_epoch(bucket):
    historical_periods = [
        f"{2021 + index // 12:04d}-{index % 12 + 1:02d}" for index in range(65)
    ]
    files = [
        *[
            _write_workbook(bucket / f"history_{index:02d}.xlsx", (period,))
            for index, period in enumerate(historical_periods, start=1)
        ],
        _write_workbook(bucket / "latest.xlsx", ("2026-06",)),
    ]
    manifest_path = _write_manifest(bucket, epoch="2026-06", files=files)

    report = _validate(bucket, manifest_path)

    assert report.epoch == "2026-06"
    assert len(report.file_rows) == 66
    print(
        "NEGATIVE_PAST_FILES_ALLOWED",
        f"historical_files={len(historical_periods)}",
        f"total_files={len(report.file_rows)}",
        f"epoch={report.epoch}",
    )


def test_collection_without_requested_epoch_stays_blocked(bucket):
    files = [_write_workbook(bucket / "only_past.xlsx", ("2026-05",))]
    manifest_path = _write_manifest(bucket, epoch="2026-06", files=files)

    with pytest.raises(G3Error, match="requested epoch 2026-06 absent from collected periods"):
        _validate(bucket, manifest_path)
    print(
        "NEGATIVE_REQUESTED_EPOCH_ABSENT_BLOCKED",
        "requested=2026-06",
        "observed=2026-05",
    )


def test_workbook_without_metric_column_fails(bucket):
    # No METRIC_MAP header in row 1 -> classify_sheet finds no metric columns ->
    # loader raises -> G3 rejects (the E-2b gap this gate closes).
    manifest_path = write_ubist_workbook_submission(bucket, include_metric=False)
    with pytest.raises(G3Error, match="structure invalid|No metric columns"):
        _validate(bucket, manifest_path)


def test_workbook_unparseable_period_fails(bucket):
    # Metric header present but row-2 period label is not "YYYY년 M월" -> that
    # metric column is dropped -> no metric columns -> structural reject.
    manifest_path = write_ubist_workbook_submission(bucket, period_labels=["July 2026"])
    with pytest.raises(G3Error, match="structure invalid|No metric columns"):
        _validate(bucket, manifest_path)


def test_workbook_requested_epoch_absent_from_collection_fails(bucket):
    # Workbook only carries 2026-06 but the manifest epoch is 2026-07.
    manifest_path = write_ubist_workbook_submission(
        bucket, epoch="2026-07", periods=("2026-06",), period_start="2026-06"
    )
    with pytest.raises(G3Error, match="requested epoch 2026-07 absent from collected periods"):
        _validate(bucket, manifest_path)


def test_workbook_newer_internal_period_becomes_output_epoch(bucket):
    manifest_path = write_ubist_workbook_submission(
        bucket, epoch="2026-07", periods=("2026-07", "2026-08")
    )
    report = _validate(bucket, manifest_path)
    assert report.epoch == "2026-08"


def test_workbook_sha_mismatch_still_fails(bucket):
    # Identity check runs before the workbook parser; a tampered file is rejected
    # on sha before we ever open it.
    manifest_path = write_ubist_workbook_submission(bucket)
    text = manifest_path.read_text(encoding="utf-8")
    import json

    data = json.loads(text)
    data["files"][0]["sha256"] = "b" * 64
    manifest_path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(G3Error, match="sha256 mismatch"):
        _validate(bucket, manifest_path)


def test_workbook_declared_rows_feed_crash_floor(bucket):
    # Declared rows are recorded so the crash floor still has a total for xlsx.
    manifest_path = write_ubist_workbook_submission(bucket, declared_rows=3)
    with pytest.raises(G3Error, match="crash floor"):
        _validate(bucket, manifest_path, previous_total_rows=1000)


def test_workbook_without_declared_rows_counts_loader_rows(bucket):
    manifest_path = write_ubist_workbook_submission(
        bucket,
        periods=("2026-07",),
        declared_rows=None,
    )

    report = _validate(bucket, manifest_path, previous_total_rows=5)

    assert report.total_rows == 3
    assert any("rows counted via loader iterator" in note for note in report.notes)


def test_good_workbook_is_fast(bucket):
    # Header-only judgment: even a small workbook must decide well under the
    # multi-second budget; this asserts we never stream data rows.
    manifest_path = write_ubist_workbook_submission(bucket, periods=("2026-07",), declared_rows=50)
    start = time.monotonic()
    _validate(bucket, manifest_path)
    assert time.monotonic() - start < 5.0
