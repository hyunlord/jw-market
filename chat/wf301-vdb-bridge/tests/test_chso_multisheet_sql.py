from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook

from src import main
from src.xlsx_preprocessor import extract_xlsx_chunks
from src.xlsx_sql_route import (
    FileSqlRouteConfig,
    SheetSqlProfile,
    classify_workbook_profiles,
    inspect_xlsx_for_sql,
    load_sql_sheet,
    logical_names_for_profiles,
    workbook_storage_route,
)


def _profile(index: int, name: str, *, columns: int = 8) -> SheetSqlProfile:
    return SheetSqlProfile(
        sheet_index=index,
        sheet_name=name,
        sheet_path=f"xl/worksheets/sheet{index}.xml",
        row_count=6_395,
        column_count=columns,
        used_cell_count=51_117,
        formula_cell_count=0,
        merged_range_count=0,
    )


def _compact_profile(
    index: int,
    name: str,
    *,
    rows: int,
    columns: int,
    used_cells: int,
) -> SheetSqlProfile:
    return SheetSqlProfile(
        sheet_index=index,
        sheet_name=name,
        sheet_path=f"xl/worksheets/sheet{index}.xml",
        row_count=rows,
        column_count=columns,
        used_cell_count=used_cells,
        formula_cell_count=0,
        merged_range_count=0,
    )


def _config() -> FileSqlRouteConfig:
    return FileSqlRouteConfig(
        enabled=True,
        min_rows=1_000,
        min_columns=8,
        max_columns=1_900,
        min_used_cells=20_000,
        min_density=0.10,
        max_merged_ranges=0,
    )


def test_chso_eight_column_dense_sheet_is_sql_candidate() -> None:
    decision = classify_workbook_profiles(
        (_profile(1, "LIVALO Market"), _profile(2, "Seven Columns", columns=7)),
        _config(),
    )

    assert decision.route == "sql"
    assert [sheet.sheet_name for sheet in decision.selected_sheets] == ["LIVALO Market"]


def test_chso_wide_sheet_preserves_all_252_columns_including_last_measure(
    tmp_path: Path,
) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Sell Out Standard"
    headers = ["AUDIT DESC", "MFR NAME KOR", "PRODUCT NAME KOR"]
    headers.extend(f"COLUMN {index}" for index in range(4, 252))
    headers.append("VALUES LC SI PRICE\n1/2026")
    sheet.append(headers)
    sheet.append(["Sell_Out", "DONG-A", "LIVALO", *range(4, 253)])
    path = tmp_path / "chso-wide.xlsx"
    workbook.save(path)

    decision = inspect_xlsx_for_sql(path, _config())
    sql_sheet = load_sql_sheet(path, decision.selected_sheets[0])

    assert decision.route == "sql"
    assert len(sql_sheet.columns) == 252
    assert sql_sheet.columns[:3] == (
        "AUDIT DESC",
        "MFR NAME KOR",
        "PRODUCT NAME KOR",
    )
    assert sql_sheet.columns[-1] == "VALUES LC SI PRICE\n1/2026"
    assert len(next(sql_sheet.rows())) == 252


def test_compact_tabular_sheets_are_sql_candidates_but_prose_sheet_stays_vdb() -> None:
    decision = classify_workbook_profiles(
        (
            _compact_profile(1, "Questions", rows=15, columns=10, used_cells=141),
            _compact_profile(2, "Coverage", rows=27, columns=5, used_cells=135),
            _compact_profile(3, "Notes", rows=27, columns=1, used_cells=22),
        ),
        _config(),
    )

    assert decision.route == "sql"
    assert [sheet.sheet_name for sheet in decision.selected_sheets] == [
        "Questions",
        "Coverage",
    ]


def test_default_column_floor_is_eight(monkeypatch) -> None:
    monkeypatch.delenv("FILE_SQL_ROUTE_MIN_COLUMNS", raising=False)

    assert FileSqlRouteConfig.from_env().min_columns == 8


def test_selected_sql_sheets_are_excluded_from_residual_vdb_chunks(tmp_path: Path) -> None:
    workbook = Workbook()
    sql_sheet = workbook.active
    sql_sheet.title = "LIVALO Market"
    sql_sheet.append(["brand", "sales"])
    sql_sheet.append(["LIVALO", 100])
    residual_sheet = workbook.create_sheet("Overview")
    residual_sheet.append(["metric", "value"])
    residual_sheet.append(["share", "3.81%"])
    path = tmp_path / "mixed.xlsx"
    workbook.save(path)

    chunks = extract_xlsx_chunks(
        path,
        exclude_sheet_names=frozenset({"LIVALO Market"}),
    )

    assert chunks
    assert all("LIVALO Market" not in chunk for chunk in chunks)
    assert any("Overview" in chunk and "3.81%" in chunk for chunk in chunks)


def test_logical_names_are_readable_and_collision_safe() -> None:
    names = logical_names_for_profiles(
        (
            _profile(1, "LIVALO Market"),
            _profile(2, "LIVALO-Market"),
            _profile(3, "PPI Market"),
        ),
        scope_prefix="doc_42",
    )

    assert names == (
        "doc_42_livalo_market",
        "doc_42_livalo_market_2",
        "doc_42_ppi_market",
    )


def test_workbook_storage_route_preserves_both_sql_and_vdb() -> None:
    assert workbook_storage_route(has_sql=True, vdb_chunk_count=1_246) == "hybrid"
    assert workbook_storage_route(has_sql=True, vdb_chunk_count=0) == "sql"
    assert workbook_storage_route(has_sql=False, vdb_chunk_count=1_246) == "vdb"


def test_hybrid_document_exposes_sql_sources_while_remaining_vdb_searchable() -> None:
    rows = [
        {
            "document_id": 42,
            "file_name": "channel-dynamics.xlsx",
            "storage_route": "hybrid",
            "sql_tables": [
                {
                    "logical_name": "doc_42_livalo_market",
                    "sheet_name": "LIVALO Market",
                    "row_count": 6_395,
                    "column_count": 8,
                }
            ],
        }
    ]

    sources = main._sql_sources_from_rows(rows)
    vdb_rows = [row for row in rows if row.get("storage_route") != "sql"]

    assert [source.logical_name for source in sources] == ["doc_42_livalo_market"]
    assert vdb_rows == rows
