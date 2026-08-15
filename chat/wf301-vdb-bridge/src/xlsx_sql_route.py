"""Deterministic XLSX routing and streaming rows for session-local SQL."""

from __future__ import annotations

import logging
import os
import re
from codecs import getincrementaldecoder
from dataclasses import dataclass
from hashlib import sha256
from html import unescape
from itertools import islice
from pathlib import Path
from typing import Iterator, Literal
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from .xlsx_preprocessor import (
    CROSSTABLE_LINK_RE,
    CROSSTABLE_MIN_INDEX_LINKS,
    SHEET_NS,
    SheetFeatures,
    SheetRow,
    XlsxPreprocessError,
    _cell_range,
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


logger = logging.getLogger(__name__)


def _env_bytes(name: str, default: int) -> int:
    """Read a byte threshold from the environment, refusing unusable values."""
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "xlsx_sql_route threshold ignored name=%s value=%r reason=not_an_integer",
            name,
            raw,
        )
        return default
    if value <= 0:
        logger.warning(
            "xlsx_sql_route threshold ignored name=%s value=%d reason=not_positive",
            name,
            value,
        )
        return default
    return value


# Byte thresholds for the in-memory dense fast path. They are settings-injected
# because the workbooks that approach them grow a column block every month, and
# the cost of raising them is memory only the operator can budget.
FAST_SQL_PROFILE_MIN_XML_BYTES = _env_bytes(
    "FILE_SQL_FAST_PROFILE_MIN_XML_BYTES", 32 * 1024 * 1024
)
FAST_SQL_PROFILE_MAX_XML_BYTES = _env_bytes(
    "FILE_SQL_FAST_PROFILE_MAX_XML_BYTES", 256 * 1024 * 1024
)
DENSE_SQL_MAX_XML_BYTES = _env_bytes(
    "FILE_SQL_DENSE_MAX_XML_BYTES", 128 * 1024 * 1024
)
_DIMENSION_REF_RE = re.compile(rb'<dimension\b[^>]*\bref="([^"]+)"')
_ROW_REF_RE = re.compile(rb'<row\b[^>]*\br="([0-9]+)"')
_ROW_SPAN_RE = re.compile(rb'<row\b[^>]*\bspans="([0-9]+):([0-9]+)"')
_EMPTY_VALUE_RE = re.compile(rb"<v(?:\s[^>]*)?>\s*</v>|<v(?:\s[^>]*)?\s*/>")
_FORMULA_TAG_RE = re.compile(rb"<f(?:\s|/?>)")
_DENSE_VALUE_CELL_RE = re.compile(
    rb'<c r="([A-Z]+)([0-9]+)"(?: s="[0-9]+")?(?: t="n")?\s*>'
    rb"\s*<v>(.*?)</v>\s*</c>"
    rb'|<c r="([A-Z]+)([0-9]+)"(?: s="[0-9]+")? t="inlineStr"\s*>'
    rb"\s*<is>(.*?)</is>\s*</c>",
    re.DOTALL,
)
_DENSE_TEXT_RE = re.compile(rb"<t(?:\s[^>]*)?>(.*?)</t>", re.DOTALL)
_UNSAFE_DENSE_XML_MARKERS = (b"<!--", b"<![CDATA[", b"<!DOCTYPE")


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
    proven_dense: bool = False
    dense_xml_sha256: str | None = None

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
    dense_sheet_xml: bytes | None = None

    def rows(self) -> Iterator[tuple[str | None, ...]]:
        if self.dense_sheet_xml is not None:
            yield from self._data_rows(_iter_dense_sheet_rows(self.dense_sheet_xml))
            return
        with ZipFile(self.path) as archive:
            shared_strings = _shared_strings(archive)
            style_formats = _style_formats(archive)
            row_iter = _iter_sheet_rows(
                archive,
                self.profile.sheet_path,
                shared_strings,
                style_formats,
            )
            yield from self._data_rows(row_iter)

    def _data_rows(
        self, row_iter: Iterator[SheetRow]
    ) -> Iterator[tuple[str | None, ...]]:
        for row in islice(row_iter, self.header_nonempty_index + 1, None):
            values = row.values[: len(self.columns)]
            if len(values) < len(self.columns):
                values.extend("" for _ in range(len(self.columns) - len(values)))
            yield tuple(value if value != "" else None for value in values)


@dataclass(frozen=True, slots=True)
class _ProvenSheetFeatures(SheetFeatures):
    dense_xml_sha256: str


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

    selected = tuple(
        profile
        for profile in profiles
        if _is_sql_candidate(profile, config)
        or _is_compact_sql_candidate(profile, config)
    )
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


def _is_compact_sql_candidate(
    profile: SheetSqlProfile,
    config: FileSqlRouteConfig,
) -> bool:
    """Keep small rectangular tables queryable without routing prose sheets to SQL."""
    return (
        profile.row_count >= 2
        and profile.row_count < config.min_rows
        and profile.column_count >= 2
        and profile.used_cell_count >= 4
        and profile.density >= 0.5
        and profile.merged_range_count == 0
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
                xml_size = archive.getinfo(sheet_path).file_size
                fast_features = (
                    _fast_sheet_features(archive, sheet_path)
                    if FAST_SQL_PROFILE_MIN_XML_BYTES
                    <= xml_size
                    <= FAST_SQL_PROFILE_MAX_XML_BYTES
                    else None
                )
                features = fast_features
                if features is None:
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
                        proven_dense=fast_features is not None,
                        dense_xml_sha256=(
                            getattr(fast_features, "dense_xml_sha256", None)
                            if fast_features is not None
                            else None
                        ),
                    )
                )
    except (BadZipFile, ElementTree.ParseError, KeyError, OSError) as exc:
        raise XlsxPreprocessError(f"xlsx SQL inspection failed: {exc}") from exc
    return classify_workbook_profiles(tuple(profiles), route_config)


def _fast_sheet_features(
    archive: ZipFile, sheet_path: str
) -> _ProvenSheetFeatures | None:
    """Profile a proven dense, formula-free OOXML sheet with C-level byte scans.

    Worksheet dimensions and per-row spans are treated only as consistency proofs.
    Any unsupported or ambiguous representation falls back to the structured parser.
    """

    raw = archive.read(sheet_path)
    if any(marker in raw for marker in _UNSAFE_DENSE_XML_MARKERS):
        return None
    if not _is_valid_utf8(raw):
        return None
    dimension_match = _DIMENSION_REF_RE.search(raw)
    if dimension_match is None or _FORMULA_TAG_RE.search(raw):
        return None
    if _EMPTY_VALUE_RE.search(raw) or b"<v " in raw or b"<is " in raw:
        return None

    try:
        dimension = dimension_match.group(1).decode("ascii", errors="strict")
    except UnicodeDecodeError:
        return None
    bounds = _cell_range(dimension)
    if bounds is None:
        row, column = _cell_position(dimension, 0, 0)
        if row < 1 or column < 1:
            return None
        max_row, max_column = row, column
    else:
        _first_row, _first_column, max_row, max_column = bounds

    row_refs = _ROW_REF_RE.findall(raw)
    row_spans = _ROW_SPAN_RE.findall(raw)
    if not row_refs or len(row_refs) != len(row_spans):
        return None
    row_numbers = tuple(int(value) for value in row_refs)
    if row_numbers != tuple(range(1, max_row + 1)):
        return None
    if any(int(start) != 1 or int(end) != max_column for start, end in row_spans):
        return None

    value_count = raw.count(b"<v>")
    inline_string_count = raw.count(b"<is>")
    used_cell_count = value_count + inline_string_count
    closed_cell_count = raw.count(b"</c>")
    if used_cell_count > closed_cell_count or used_cell_count > max_row * max_column:
        return None

    return _ProvenSheetFeatures(
        row_count=max_row,
        column_count=max_column,
        used_cell_count=used_cell_count,
        formula_cell_count=0,
        merged_range_count=raw.count(b"<mergeCell "),
        dense_xml_sha256=sha256(raw).hexdigest(),
    )


def load_sql_sheet(path: Path, profile: SheetSqlProfile) -> SqlSheetData:
    try:
        with ZipFile(path) as archive:
            shared_strings = _shared_strings(archive)
            style_formats = _style_formats(archive)
            dense_sheet_xml = _validated_dense_sheet_xml(
                archive,
                profile,
                shared_strings=shared_strings,
                style_formats=style_formats,
            )
            row_iter = (
                _iter_dense_sheet_rows(dense_sheet_xml)
                if dense_sheet_xml is not None
                else _iter_sheet_rows(
                    archive,
                    profile.sheet_path,
                    shared_strings,
                    style_formats,
                )
            )
            first_rows = list(islice(row_iter, 10))
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
        dense_sheet_xml=dense_sheet_xml,
    )


def _validated_dense_sheet_xml(
    archive: ZipFile,
    profile: SheetSqlProfile,
    *,
    shared_strings: list[str],
    style_formats: dict[int, str],
) -> bytes | None:
    if not profile.proven_dense or shared_strings or style_formats:
        _log_dense_declined(profile, "sheet_not_proven_dense")
        return None
    xml_bytes = archive.getinfo(profile.sheet_path).file_size
    if xml_bytes > DENSE_SQL_MAX_XML_BYTES:
        # Not a failure: the streaming parser still reads every row. Logged so a
        # workbook that crosses the threshold is visible before it is a mystery.
        _log_dense_declined(
            profile,
            "xml_over_dense_limit",
            xml_bytes=xml_bytes,
            limit_bytes=DENSE_SQL_MAX_XML_BYTES,
        )
        return None
    raw = archive.read(profile.sheet_path)
    if profile.dense_xml_sha256 is not None:
        if sha256(raw).hexdigest() == profile.dense_xml_sha256:
            return raw
        _log_dense_declined(profile, "dense_xml_digest_changed")
        return None
    if any(marker in raw for marker in _UNSAFE_DENSE_XML_MARKERS):
        _log_dense_declined(profile, "unsupported_xml_markers")
        return None
    if not _is_valid_utf8(raw):
        _log_dense_declined(profile, "xml_not_valid_utf8")
        return None
    matched_cells = sum(1 for _match in _DENSE_VALUE_CELL_RE.finditer(raw))
    if matched_cells != profile.used_cell_count:
        _log_dense_declined(
            profile,
            "dense_cell_count_mismatch",
            matched_cells=matched_cells,
            profiled_cells=profile.used_cell_count,
        )
        return None
    return raw


def _log_dense_declined(
    profile: SheetSqlProfile, reason: str, **details: int
) -> None:
    """Record why the dense fast path was declined for a sheet.

    Declining is safe — the streaming parser produces identical rows — but it is
    a silent performance cliff unless the reason is written down.
    """

    extra = "".join(f" {key}={value}" for key, value in sorted(details.items()))
    logger.info(
        "xlsx_sql_route dense_path_declined sheet=%s reason=%s fallback=streaming_parser%s",
        profile.sheet_name,
        reason,
        extra,
    )


def _is_valid_utf8(raw: bytes, *, chunk_size: int = 1024 * 1024) -> bool:
    decoder = getincrementaldecoder("utf-8")()
    try:
        for offset in range(0, len(raw), chunk_size):
            decoder.decode(raw[offset : offset + chunk_size], final=False)
        decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        return False
    return True


def _iter_dense_sheet_rows(raw: bytes) -> Iterator[SheetRow]:
    column_numbers: dict[bytes, int] = {}
    current_row = 0
    values: list[str] = []
    for match in _DENSE_VALUE_CELL_RE.finditer(raw):
        column_letters = match.group(1) or match.group(4)
        row_number = int(match.group(2) or match.group(5))
        if current_row and row_number != current_row:
            if any(value.strip() for value in values):
                yield SheetRow(row_number=current_row, values=values)
            values = []
        current_row = row_number

        column_number = column_numbers.get(column_letters)
        if column_number is None:
            column_number = 0
            for byte in column_letters:
                column_number = column_number * 26 + byte - 64
            column_numbers[column_letters] = column_number

        raw_number = match.group(3)
        if raw_number is not None:
            value = " ".join(raw_number.decode("utf-8").replace("\x00", " ").split())
        else:
            inline_xml = match.group(6) or b""
            parts = (
                _decode_xml_text(item) for item in _DENSE_TEXT_RE.findall(inline_xml)
            )
            value = _clean_dense_cell_text("".join(parts))
        _set_row_value(values, column_number, value)

    if current_row and any(value.strip() for value in values):
        yield SheetRow(row_number=current_row, values=values)


def _decode_xml_text(value: bytes) -> str:
    text = value.decode("utf-8")
    return unescape(text) if "&" in text else text


def _clean_dense_cell_text(value: str) -> str:
    lines = [" ".join(line.split()) for line in value.replace("\x00", " ").splitlines()]
    return "\n".join(line for line in lines if line)


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
