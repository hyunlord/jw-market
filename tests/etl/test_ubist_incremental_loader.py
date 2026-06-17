from __future__ import annotations

import json
from pathlib import Path

import openpyxl
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

import pytest

from pipeline.etl.io.ubist_loader import (
    BUSINESS_GRAIN_COLUMNS,
    COLUMNS,
    SCHEMA,
    deduplicate_business_grain,
    incremental_plan,
    run_incremental_ubist_load,
)


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
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Sheet1"
    sheet.cell(1, 1).value = None
    sheet.cell(2, 1).value = "제품"
    for idx, period in enumerate(periods, start=2):
        year, month = period.split("-")
        sheet.cell(1, idx).value = "처방조제액(원)"
        sheet.cell(2, idx).value = f"{year}년 {int(month)}월"
        sheet.cell(3, idx).value = 1
    sheet.cell(3, 1).value = "테스트"
    workbook.save(path)
    workbook.close()
    return path


def _ubist_row(**overrides):
    row = dict.fromkeys(COLUMNS)
    row.update(
        {
            "제품": "테스트제품",
            "ATC": "A10BA",
            "브랜드": "테스트브랜드",
            "성분": "테스트성분",
            "약품코드": "P001",
            "제형": "정제",
            "투여경로": "경구",
            "급여구분": "급여",
            "종별": "의원",
            "진료과": "내과",
            "연령": "전체",
            "성별": "전체",
            "period_yyyymm": "2026-02",
            "rx_amt": 100.0,
            "rx_cnt": 10.0,
            "rx_qty": 20.0,
            "source_file": "a.xlsx",
            "source_folder": "A",
            "source_sheet": "Sheet1",
            "source_row_no": 3,
            "ingested_at": "2026-06-17T00:00:00",
        }
    )
    row.update(overrides)
    return row


def _write_partition(target: Path, period: str, rows: list[dict[str, object]]) -> None:
    year, month = period.split("-")
    path = target / f"year={year}" / f"month={month}" / "data.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pandas(pd.DataFrame(rows).reindex(columns=COLUMNS), schema=SCHEMA, preserve_index=False)
    pq.write_table(table, path)


def test_business_grain_dedup_ignores_static_metadata_and_prefers_populated_values():
    rows = [
        _ubist_row(source_file="a.xlsx", source_row_no=4),
        _ubist_row(source_file="b.xlsx", source_row_no=3, PMS만료일="2030-01-01", Generic="Original"),
    ]

    deduped, report = deduplicate_business_grain(pd.DataFrame(rows), "2026-02")

    assert len(deduped) == 1
    assert report.duplicate_groups == 1
    assert report.duplicate_rows_removed == 1
    assert deduped.iloc[0]["source_file"] == "b.xlsx"
    assert deduped.iloc[0]["PMS만료일"] == "2030-01-01"
    assert deduped.iloc[0]["Generic"] == "Original"


def test_business_grain_dedup_preserves_metric_conflicts():
    rows = [
        _ubist_row(source_file="a.xlsx", rx_amt=100.0),
        _ubist_row(source_file="b.xlsx", rx_amt=101.0),
    ]

    deduped, report = deduplicate_business_grain(pd.DataFrame(rows), "2026-02")

    assert len(deduped) == 2
    assert report.duplicate_rows_removed == 0
    assert report.conflict_groups == 1
    assert report.conflict_rows == 2


def test_incremental_plan_skips_manifest_source_files_and_adds_new_ones(tmp_path):
    target = tmp_path / "target"
    _write_manifest(target, ["loaded.xlsx"])
    loaded = _workbook(tmp_path / "loaded.xlsx", ["2025-07"])
    new = _workbook(tmp_path / "new.xlsx", ["2025-08"])

    plan = incremental_plan([loaded, new], target)

    assert [summary.source_file for summary in plan.skip] == ["loaded.xlsx"]
    assert [summary.source_file for summary in plan.add] == ["new.xlsx"]
    assert plan.conflicts == []


def test_incremental_plan_reports_content_overlap_across_folders(tmp_path):
    target = tmp_path / "target"
    _write_manifest(target, [])
    _write_partition(
        target,
        "2025-07",
        [
            _ubist_row(
                **{col: None for col in BUSINESS_GRAIN_COLUMNS if col not in {"제품", "period_yyyymm"}},
                제품="테스트",
                period_yyyymm="2025-07",
                rx_amt=1.0,
                rx_cnt=None,
                rx_qty=None,
                source_file="old-source.xlsx",
            )
        ],
    )
    new = _workbook(tmp_path / "other-folder" / "new-source.xlsx", ["2025-07"])

    plan = incremental_plan([new], target)

    assert len(plan.conflicts) == 1
    assert plan.conflicts[0]["period_yyyymm"] == "2025-07"
    assert plan.conflicts[0]["reason"] == "content-level fact+metric overlap"
    assert plan.conflicts[0]["left"] == "old-source.xlsx"
    assert plan.conflicts[0]["right"] == "new-source.xlsx"


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
