from __future__ import annotations

import json
from pathlib import Path

import openpyxl

import pytest

from pipeline.etl.io.ubist_loader import incremental_plan, run_incremental_ubist_load


def _write_manifest(target: Path, source_files: list[str]) -> None:
    target.mkdir(parents=True)
    (target / "_manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "partitions": [
                    {
                        "period_yyyymm": "2025-07",
                        "path": "year=2025/month=07/data.parquet",
                        "row_count": 1,
                        "source_files": source_files,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _workbook(path: Path, periods: list[str]) -> Path:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Sheet1"
    sheet.cell(1, 1).value = None
    sheet.cell(2, 1).value = "제품"
    for idx, period in enumerate(periods, start=2):
        year, month = period.split("-")
        sheet.cell(1, idx).value = "처방조제액(원)"
        sheet.cell(2, idx).value = f"{year}년 {int(month)}월"
    sheet.cell(3, 1).value = "테스트"
    workbook.save(path)
    workbook.close()
    return path


def test_incremental_plan_skips_manifest_source_files_and_adds_new_ones(tmp_path):
    target = tmp_path / "target"
    _write_manifest(target, ["loaded.xlsx"])
    loaded = _workbook(tmp_path / "loaded.xlsx", ["2025-07"])
    new = _workbook(tmp_path / "new.xlsx", ["2025-08"])

    plan = incremental_plan([loaded, new], target)

    assert [summary.source_file for summary in plan.skip] == ["loaded.xlsx"]
    assert [summary.source_file for summary in plan.add] == ["new.xlsx"]
    assert plan.conflicts == []


def test_incremental_plan_reports_same_folder_period_overlap(tmp_path):
    target = tmp_path / "target"
    _write_manifest(target, [])
    first = _workbook(tmp_path / "종병 2501-07.xlsx", ["2025-07"])
    second = _workbook(tmp_path / "종병 2507-12.xlsx", ["2025-07", "2025-08"])

    plan = incremental_plan([first, second], target)

    assert len(plan.conflicts) == 1
    assert plan.conflicts[0]["period_yyyymm"] == "2025-07"
    assert plan.conflicts[0]["left"] == "종병 2501-07.xlsx"
    assert plan.conflicts[0]["right"] == "종병 2507-12.xlsx"


def test_incremental_load_stops_on_period_overlap_by_default(tmp_path):
    target = tmp_path / "target"
    _write_manifest(target, [])
    first = _workbook(tmp_path / "종병 2501-07.xlsx", ["2025-07"])
    second = _workbook(tmp_path / "종병 2507-12.xlsx", ["2025-07", "2025-08"])

    with pytest.raises(RuntimeError, match="period conflicts"):
        run_incremental_ubist_load(target=target, paths=[first, second])


def test_incremental_load_allows_period_overlap_when_dedup_enabled(tmp_path, monkeypatch):
    target = tmp_path / "target"
    _write_manifest(target, [])
    first = _workbook(tmp_path / "종병 2501-07.xlsx", ["2025-07"])
    second = _workbook(tmp_path / "종병 2507-12.xlsx", ["2025-07", "2025-08"])
    loaded_paths: list[Path] = []

    def fake_load_to_parquet(paths, target, *, mode, truncate, previous_manifest):
        loaded_paths.extend(paths)
        return {}

    monkeypatch.setattr("pipeline.etl.io.ubist_loader.load_to_parquet", fake_load_to_parquet)

    run_incremental_ubist_load(target=target, paths=[first, second], allow_overlap_dedup=True)

    assert loaded_paths == [first.resolve(), second.resolve()]
