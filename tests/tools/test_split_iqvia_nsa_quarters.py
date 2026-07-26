from __future__ import annotations

import ast
import inspect
from pathlib import Path

import openpyxl

from pipeline.etl.io.iqvia_loader import iter_nsa_xlsx
from pipeline.scripts.ingest_hook import rehearsal_nsa_split
from pipeline.scripts.ingest_hook.rehearsal_nsa_split import split_workbook


def _long_nsa(path: Path) -> None:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "arbitrary"
    sheet.append(
        [
            "AUDIT CODE",
            "MFR CODE",
            "PRODUCT NAME",
            "PACK DESC",
            "DATA PERIOD",
            "Values LC",
            "Units",
            "Counting Units",
            "Dosage Units",
            "Price",
        ]
    )
    for year, month in ((2025, 12), (2026, 3), (2026, 6)):
        sheet.append(
            [
                f"A-{year}-{month}",
                "M",
                "Brand",
                "Pack",
                f"{year}-{month:02d}",
                1,
                2,
                3,
                4,
                5,
            ]
        )
    workbook.save(path)


def _twenty_quarter_nsa(path: Path) -> tuple[str, ...]:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Not A Canonical Sheet"
    sheet.append(
        [
            "AUDIT CODE",
            "MFR CODE",
            "PRODUCT NAME",
            "PACK DESC",
            "DATA PERIOD",
            "Values LC",
            "Units",
            "Counting Units",
            "Dosage Units",
            "Price",
        ]
    )
    periods: list[str] = []
    for index in range(20):
        year = 2021 + index // 4
        quarter = index % 4 + 1
        month = quarter * 3
        periods.append(f"{year}-Q{quarter}")
        sheet.append(
            [
                f"A-{index:02d}",
                "M",
                "Brand",
                "Pack",
                f"{year}-{month:02d}",
                index + 1,
                index + 2,
                index + 3,
                index + 4,
                index + 5,
            ]
        )
    workbook.save(path)
    return tuple(periods)


def test_split_long_workbook_keeps_latest_quarter_separate(tmp_path: Path) -> None:
    source = tmp_path / "not-canonical-name.bin"
    first = tmp_path / "first-quarters.random"
    latest = tmp_path / "latest-quarter.random"
    _long_nsa(source)

    result = split_workbook(source, first, latest, history_quarters=19)

    assert result.latest_quarters == ("2026-Q2",)
    assert result.history_quarters == ("2025-Q4", "2026-Q1")
    assert {
        f"{row['period_yyyy']}-Q{row['period_quarter']}"
        for row in iter_nsa_xlsx(first)
    } == {"2025-Q4", "2026-Q1"}
    assert {
        f"{row['period_yyyy']}-Q{row['period_quarter']}"
        for row in iter_nsa_xlsx(latest)
    } == {"2026-Q2"}


def test_split_uses_19_plus_1_quarter_window_and_is_repeatable(
    tmp_path: Path,
) -> None:
    source = tmp_path / "structure-only.payload"
    history = tmp_path / "history.xlsx"
    latest = tmp_path / "latest.xlsx"
    periods = _twenty_quarter_nsa(source)

    first = split_workbook(source, history, latest, history_quarters=19)
    second = split_workbook(source, history, latest, history_quarters=19)

    assert first == second
    assert first.history_quarters == periods[:19]
    assert first.latest_quarters == (periods[-1],)
    assert first.history_rows == 19
    assert first.latest_rows == 1
    assert {
        f"{row['period_yyyy']}-Q{row['period_quarter']}"
        for row in iter_nsa_xlsx(history)
    } == set(periods[:19])
    assert {
        f"{row['period_yyyy']}-Q{row['period_quarter']}"
        for row in iter_nsa_xlsx(latest)
    } == {periods[-1]}


def test_split_uses_streaming_output_workbooks(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source.xlsx"
    history = tmp_path / "history.xlsx"
    latest = tmp_path / "latest.xlsx"
    _long_nsa(source)
    calls: list[bool] = []
    original = openpyxl.Workbook

    def tracking_workbook(*args, **kwargs):
        calls.append(bool(kwargs.get("write_only")))
        return original(*args, **kwargs)

    monkeypatch.setattr(rehearsal_nsa_split.openpyxl, "Workbook", tracking_workbook)

    split_workbook(source, history, latest)

    assert calls == [True, True]


def test_split_does_not_materialize_source_rows() -> None:
    tree = ast.parse(inspect.getsource(split_workbook))
    comprehensions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp))
    ]
    assert comprehensions == []
