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

import pytest

from pipeline.scripts.ingest_hook.category_map import resolve_category
from pipeline.scripts.ingest_hook.contract import load_manifest
from pipeline.scripts.ingest_hook.g3 import G3Error, validate
from ingest_fixtures import write_ubist_workbook_submission

SPEC = resolve_category("ubist")


def _validate(bucket, manifest_path, **kwargs):
    return validate(load_manifest(manifest_path), SPEC, bucket, **kwargs)


def test_good_workbook_passes(bucket):
    manifest_path = write_ubist_workbook_submission(bucket, periods=("2026-06", "2026-07"))
    report = _validate(bucket, manifest_path)
    assert "2026-07" in report.observed_periods
    assert any("workbook validated via loader parser" in note for note in report.notes)


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


def test_workbook_epoch_absent_fails(bucket):
    # Workbook only carries 2026-06 but the manifest epoch is 2026-07.
    manifest_path = write_ubist_workbook_submission(
        bucket, epoch="2026-07", periods=("2026-06",), period_start="2026-06"
    )
    with pytest.raises(G3Error, match="absent from workbook periods"):
        _validate(bucket, manifest_path)


def test_workbook_future_period_fails(bucket):
    manifest_path = write_ubist_workbook_submission(
        bucket, epoch="2026-07", periods=("2026-07", "2026-08")
    )
    with pytest.raises(G3Error, match="beyond epoch"):
        _validate(bucket, manifest_path)


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


def test_good_workbook_is_fast(bucket):
    # Header-only judgment: even a small workbook must decide well under the
    # multi-second budget; this asserts we never stream data rows.
    manifest_path = write_ubist_workbook_submission(bucket, periods=("2026-07",), declared_rows=50)
    start = time.monotonic()
    _validate(bucket, manifest_path)
    assert time.monotonic() - start < 5.0


def test_workbook_content_is_independent_of_extension(bucket):
    manifest_path = write_ubist_workbook_submission(bucket)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    original = bucket / manifest["files"][0]["path"]
    renamed = original.with_suffix(".payload")
    original.rename(renamed)
    manifest["files"][0]["path"] = renamed.relative_to(bucket).as_posix()
    manifest["files"][0]["sha256"] = hashlib.sha256(renamed.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = _validate(bucket, manifest_path)

    assert report.category == "ubist"


def test_fake_xlsx_container_is_rejected(bucket):
    manifest_path = write_ubist_workbook_submission(bucket)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    workbook = bucket / manifest["files"][0]["path"]
    workbook.write_text("period,brand,value\n2026-07,Drug A,1\n", encoding="utf-8")
    manifest["files"][0]["sha256"] = hashlib.sha256(workbook.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(G3Error, match="does not contain an Office workbook"):
        _validate(bucket, manifest_path)
