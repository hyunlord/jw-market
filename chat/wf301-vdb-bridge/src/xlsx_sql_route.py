"""Deterministic XLSX routing and streaming rows for session-local SQL."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Iterator, Literal
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from .xlsx_preprocessor import (
    CROSSTABLE_LINK_RE,
    CROSSTABLE_MIN_INDEX_LINKS,
    SHEET_NS,
    SheetRow,
    XlsxPreprocessError,
    _cell_position,
    _cell_value,
    _dedupe_headers,
    _first_header,
    _set_row_value,
    _shared_strings,
    _sheet_features_streaming,
    _sheet_paths,
    _style_formats,
)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


@dataclass(frozen=True, slots=True)
class FileSqlRouteConfig:
    enabled: bool
    min_rows: int
    min_columns: int
    max_columns: int
    min_used_cells: int
    min_density: float
    max_merged_ranges: int

    @classmethod
    def from_env(cls) -> FileSqlRouteConfig:
        return cls(
            enabled=os.environ.get("FILE_SQL_ENABLED", "false").lower()
            in {"1", "true", "yes", "on"},
            min_rows=_env_int("FILE_SQL_ROUTE_MIN_ROWS", 1_000),
            min_columns=_env_int("FILE_SQL_ROUTE_MIN_COLUMNS", 8),
            max_columns=_env_int("FILE_SQL_ROUTE_MAX_COLUMNS", 1_900),
            min_used_cells=_env_int("FILE_SQL_ROUTE_MIN_USED_CELLS", 20_000),
            min_density=_env_float("FILE_SQL_ROUTE_MIN_DENSITY", 0.10),
            max_merged_ranges=_env_int("FILE_SQL_ROUTE_MAX_MERGED_RANGES", 0),
        )


@dataclass(frozen=True, slots=True)
class SheetSqlProfile:
    sheet_index: int
    sheet_name: str
    sheet_path: str
    row_count: int
    column_count: int
    used_cell_count: int
    formula_cell_count: int
    merged_range_count: int
    crosstable: bool = False

    @property
    def density(self) -> float:
        area = self.row_count * self.column_count
        return self.used_cell_count / area if area else 0.0

    def audit_dict(self) -> dict[str, object]:
        return {
            "sheet_index": self.sheet_index,
            "sheet_name": self.sheet_name,
            "rows": self.row_count,
            "columns": self.column_count,
            "used_cells": self.used_cell_count,
            "formula_cells": self.formula_cell_count,
            "merged_ranges": self.merged_range_count,
            "density": round(self.density, 6),
            "crosstable": self.crosstable,
        }


@dataclass(frozen=True, slots=True)
class WorkbookSqlDecision:
    route: Literal["sql", "vdb"]
    reason: str
    profiles: tuple[SheetSqlProfile, ...]
    selected_sheets: tuple[SheetSqlProfile, ...]


@dataclass(frozen=True, slots=True)
class SqlSheetData:
    path: Path
    profile: SheetSqlProfile
    columns: tuple[str, ...]
    header_nonempty_index: int

    def rows(self) -> Iterator[tuple[str | None, ...]]:
        with ZipFile(self.path) as archive:
            shared_strings = _shared_strings(archive)
            style_formats = _style_formats(archive)
            row_iter = _iter_sheet_rows(
                archive,
                self.profile.sheet_path,
                shared_strings,
                style_formats,
            )
            for row in islice(row_iter, self.header_nonempty_index + 1, None):
                values = row.values[: len(self.columns)]
                if len(values) < len(self.columns):
                    values.extend("" for _ in range(len(self.columns) - len(values)))
                yield tuple(value if value != "" else None for value in values)


def logical_names_for_profiles(
    profiles: tuple[SheetSqlProfile, ...],
    *,
    scope_prefix: str = "",
) -> tuple[str, ...]:
    """Return deterministic, readable, workbook-local SQL table names."""
    seen: dict[str, int] = {}
    names: list[str] = []
    for profile in profiles:
        base = re.sub(r"[^\w]+", "_", profile.sheet_name.casefold()).strip("_")
        if not base:
            base = f"sheet_{profile.sheet_index}"
        occurrence = seen.get(base, 0) + 1
        seen[base] = occurrence
        unique_name = base if occurrence == 1 else f"{base}_{occurrence}"
        names.append(f"{scope_prefix}_{unique_name}" if scope_prefix else unique_name)
    return tuple(names)


def workbook_storage_route(*, has_sql: bool, vdb_chunk_count: int) -> str:
    """Describe the internal storage paths without changing the public route enum."""
    if has_sql and vdb_chunk_count > 0:
        return "hybrid"
    if has_sql:
        return "sql"
    return "vdb"


def classify_workbook_profiles(
    profiles: tuple[SheetSqlProfile, ...],
    config: FileSqlRouteConfig,
) -> WorkbookSqlDecision:
    if not config.enabled:
        return WorkbookSqlDecision(
            route="vdb",
            reason="file SQL disabled; existing VDB route preserved",
            profiles=profiles,
            selected_sheets=(),
        )

    selected = tuple(profile for profile in profiles if _is_sql_candidate(profile, config))
    if not selected:
        return WorkbookSqlDecision(
            route="vdb",
            reason=(
                "deterministic shape gate selected no dense raw sheet; "
                "existing VDB route preserved"
            ),
            profiles=profiles,
            selected_sheets=(),
        )
    selected_names = ", ".join(profile.sheet_name for profile in selected)
    return WorkbookSqlDecision(
        route="sql",
        reason=(
            "deterministic shape gate selected dense raw sheet(s): "
            f"{selected_names}; thresholds rows>={config.min_rows}, "
            f"columns={config.min_columns}..{config.max_columns}, "
            f"used_cells>={config.min_used_cells}, density>={config.min_density}, "
            f"merged_ranges<={config.max_merged_ranges}"
        ),
        profiles=profiles,
        selected_sheets=selected,
    )


def inspect_xlsx_for_sql(
    path: Path,
    config: FileSqlRouteConfig | None = None,
) -> WorkbookSqlDecision:
    route_config = config or FileSqlRouteConfig.from_env()
    try:
        with ZipFile(path) as archive:
            sheet_paths = _sheet_paths(archive)
            crosstable_names = _crosstable_sheet_names(archive, sheet_paths)
            profiles: list[SheetSqlProfile] = []
            for index, (sheet_name, sheet_path) in enumerate(sheet_paths, start=1):
                features = _sheet_features_streaming(archive, sheet_path)
                profiles.append(
                    SheetSqlProfile(
                        sheet_index=index,
                        sheet_name=sheet_name,
                        sheet_path=sheet_path,
                        row_count=features.row_count,
                        column_count=features.column_count,
                        used_cell_count=features.used_cell_count,
                        formula_cell_count=features.formula_cell_count,
                        merged_range_count=features.merged_range_count,
                        crosstable=sheet_name in crosstable_names,
                    )
                )
    except (BadZipFile, ElementTree.ParseError, KeyError, OSError) as exc:
        raise XlsxPreprocessError(f"xlsx SQL inspection failed: {exc}") from exc
    return classify_workbook_profiles(tuple(profiles), route_config)


def load_sql_sheet(path: Path, profile: SheetSqlProfile) -> SqlSheetData:
    try:
        with ZipFile(path) as archive:
            shared_strings = _shared_strings(archive)
            style_formats = _style_formats(archive)
            first_rows = list(
                islice(
                    _iter_sheet_rows(
                        archive,
                        profile.sheet_path,
                        shared_strings,
                        style_formats,
                    ),
                    10,
                )
            )
    except (BadZipFile, ElementTree.ParseError, KeyError, OSError) as exc:
        raise XlsxPreprocessError(f"xlsx SQL header read failed: {exc}") from exc
    header = _first_header([row.values for row in first_rows])
    if header is None:
        raise XlsxPreprocessError(
            f"xlsx SQL sheet has no usable header: {profile.sheet_name}"
        )
    header_index, raw_headers = header
    width = profile.column_count
    if len(raw_headers) < width:
        raw_headers = [*raw_headers, *("" for _ in range(width - len(raw_headers)))]
    columns = tuple(_dedupe_headers(raw_headers[:width]))
    return SqlSheetData(
        path=path,
        profile=profile,
        columns=columns,
        header_nonempty_index=header_index,
    )


def _is_sql_candidate(
    profile: SheetSqlProfile,
    config: FileSqlRouteConfig,
) -> bool:
    return (
        not profile.crosstable
        and profile.row_count >= config.min_rows
        and config.min_columns <= profile.column_count <= config.max_columns
        and profile.used_cell_count >= config.min_used_cells
        and profile.density >= config.min_density
        and profile.merged_range_count <= config.max_merged_ranges
    )


def _crosstable_sheet_names(
    archive: ZipFile,
    sheet_paths: list[tuple[str, str]],
) -> frozenset[str]:
    index_path = next(
        (path for name, path in sheet_paths if name.strip().upper() == "INDEX"),
        None,
    )
    if index_path is None:
        return frozenset()
    raw = archive.read(index_path).decode("utf-8", errors="ignore")
    if len(CROSSTABLE_LINK_RE.findall(raw)) < CROSSTABLE_MIN_INDEX_LINKS:
        return frozenset()
    return frozenset(
        name for name, _path in sheet_paths if name.strip().upper() in {"TABLE", "FREQ"}
    )


def _iter_sheet_rows(
    archive: ZipFile,
    sheet_path: str,
    shared_strings: list[str],
    style_formats: dict[int, str],
) -> Iterator[SheetRow]:
    fallback_row = 0
    with archive.open(sheet_path) as sheet_file:
        for event, element in ElementTree.iterparse(sheet_file, events=("end",)):
            if event != "end" or element.tag != f"{SHEET_NS}row":
                continue
            fallback_row += 1
            raw_row = str(element.attrib.get("r") or "")
            row_number = int(raw_row) if raw_row.isdigit() else fallback_row
            values: list[str] = []
            for fallback_column, cell in enumerate(
                element.findall(f"{SHEET_NS}c"), start=1
            ):
                _cell_row, column_number = _cell_position(
                    str(cell.attrib.get("r") or ""),
                    row_number,
                    fallback_column,
                )
                _set_row_value(
                    values,
                    column_number,
                    _cell_value(cell, shared_strings, style_formats),
                )
            if any(value.strip() for value in values):
                yield SheetRow(row_number=row_number, values=values)
            element.clear()
