"""G3 structural validation — pass path plus every rejection class (G-2 units)."""
from __future__ import annotations

import hashlib
import json

import openpyxl
import pytest

from pipeline.scripts.ingest_hook.category_map import resolve_category
from pipeline.scripts.ingest_hook.contract import load_manifest
from pipeline.scripts.ingest_hook.g3 import G3Error, validate
from ingest_fixtures import GOOD_ROWS, write_submission

SPEC = resolve_category("ubist")


def _validate(bucket, manifest_path, **kwargs):
    return validate(load_manifest(manifest_path), SPEC, bucket, **kwargs)


def test_good_submission_passes(bucket):
    report = _validate(bucket, write_submission(bucket))
    assert report.total_rows == len(GOOD_ROWS)
    assert any("dedup" in note for note in report.notes)


def test_missing_file_fails(bucket):
    manifest_path = write_submission(bucket)
    (bucket / "ubist" / "2026-07" / "data.csv").unlink()
    with pytest.raises(G3Error, match="not found"):
        _validate(bucket, manifest_path)


def test_sha_mismatch_fails(bucket):
    manifest_path = write_submission(bucket, sha_override="b" * 64)
    with pytest.raises(G3Error, match="sha256 mismatch"):
        _validate(bucket, manifest_path)


def test_broken_schema_fails(bucket):
    manifest_path = write_submission(bucket, header=("period", "level", "name", "amount"))
    with pytest.raises(G3Error, match="missing required columns"):
        _validate(bucket, manifest_path)


def test_zero_rows_fails(bucket):
    manifest_path = write_submission(bucket, rows=[], declared_rows=0)
    with pytest.raises(G3Error, match="zero data rows|epoch 2026-07 absent"):
        _validate(bucket, manifest_path)


def test_epoch_absent_from_file_fails(bucket):
    rows = [row for row in GOOD_ROWS if row[0] != "2026-07"]
    manifest_path = write_submission(bucket, rows=rows)
    with pytest.raises(G3Error, match="absent"):
        _validate(bucket, manifest_path)


def test_future_period_fails(bucket):
    rows = GOOD_ROWS + [("2026-08", "Class", "미래", 1.0)]
    manifest_path = write_submission(bucket, rows=rows)
    with pytest.raises(G3Error, match="beyond epoch"):
        _validate(bucket, manifest_path)


def test_declared_row_mismatch_fails(bucket):
    manifest_path = write_submission(bucket, declared_rows=999)
    with pytest.raises(G3Error, match="declares rows=999"):
        _validate(bucket, manifest_path)


def test_row_crash_vs_previous_fails(bucket):
    manifest_path = write_submission(bucket)
    with pytest.raises(G3Error, match="crash floor"):
        _validate(bucket, manifest_path, previous_total_rows=1000)


def test_row_growth_vs_previous_passes(bucket):
    report = _validate(bucket, write_submission(bucket), previous_total_rows=6)
    assert report.total_rows == 6


def test_path_escape_fails(bucket):
    manifest_path = write_submission(bucket)
    text = manifest_path.read_text(encoding="utf-8").replace("ubist/2026-07/data.csv", "../outside.csv")
    manifest_path.write_text(text, encoding="utf-8")
    with pytest.raises(G3Error, match="escapes input root"):
        _validate(bucket, manifest_path)


def _write_workbook_submission(bucket, *, metric_period: str) -> object:
    data_path = bucket / "ubist" / "2026-05" / "may.xlsx"
    data_path.parent.mkdir(parents=True, exist_ok=True)
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "UBIST"
    sheet.append((None, "처방조제액(원)"))
    sheet.append(("브랜드", metric_period))
    sheet.append(("리바로", 100.0))
    workbook.save(data_path)
    manifest = {
        "contract_version": "v2",
        "epoch": "2026-05",
        "category": "ubist",
        "complete": True,
        "submitted_at": "2026-07-18T09:00:00+09:00",
        "files": [
            {
                "path": data_path.relative_to(bucket).as_posix(),
                "sha256": hashlib.sha256(data_path.read_bytes()).hexdigest(),
                "rows": 1,
                "period_start": "2026-05",
                "period_end": "2026-05",
            }
        ],
    }
    manifest_path = data_path.with_name("manifest.json")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path


def test_ubist_workbook_checks_actual_period_and_rows(bucket):
    report = _validate(bucket, _write_workbook_submission(bucket, metric_period="2026년 5월"))

    assert report.total_rows == 1
    assert report.observed_periods == {"2026-05"}


def test_ubist_workbook_rejects_manifest_epoch_absent_from_headers(bucket):
    manifest_path = _write_workbook_submission(bucket, metric_period="2026년 4월")

    with pytest.raises(G3Error, match="epoch 2026-05 absent"):
        _validate(bucket, manifest_path)


WEEKLY_ROWS = [
    ("2026-W26", "Class", "리바로", 5.0),
    ("2026-W26", "전체", "-", 5.0),
    ("2026-W27", "Class", "리바로", 7.0),
    ("2026-W27", "전체", "-", 7.0),
]


def test_weekly_epoch_period_consistency_passes(bucket):
    manifest_path = write_submission(
        bucket, epoch="2026-W27", rows=WEEKLY_ROWS, period_start="2026-W26"
    )
    report = _validate(bucket, manifest_path)
    assert report.epoch == "2026-W27"
    assert report.total_rows == len(WEEKLY_ROWS)


def test_weekly_epoch_future_week_fails(bucket):
    rows = WEEKLY_ROWS + [("2026-W28", "Class", "미래", 1.0)]
    manifest_path = write_submission(
        bucket, epoch="2026-W27", rows=rows, period_start="2026-W26"
    )
    with pytest.raises(G3Error, match="beyond epoch"):
        _validate(bucket, manifest_path)
