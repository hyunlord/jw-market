"""XLSX-specific text extraction for wf301 uploaded market tables."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from itertools import chain
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Callable, Final, Iterable, Iterator
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


SHEET_NS: Final = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL_NS: Final = "{http://schemas.openxmlformats.org/package/2006/relationships}"
OFFICE_REL_NS: Final = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
DEFAULT_CHUNK_CHAR_LIMIT: Final = 1800
# 다행 묶음(헤더 1회 + N행) 상한. 문자 예산(chunk_char_limit 1800자)이 1차 경계이고,
# 이 상한은 한 자릿수 초소형 행(1800/9자 ≈ 200행)이 한 청크에 몰리는 병리 케이스만 막는
# 2차 안전장치다. 기존(행별 헤더 반복) 패킹도 초소형 행은 청크당 ~100행이었다.
XLSX_BUNDLE_MAX_ROWS: Final = _env_int("XLSX_BUNDLE_MAX_ROWS", 200)
# 문맥(메타데이터+표머리) 뒤에 최소 1개 데이터 행이 들어갈 여유가 없으면 묶음 대신
# 기존 행별 헤더-값 쌍 형식으로 되돌린다(초광폭 시트 잘림 방지 경로 유지).
BUNDLE_MIN_ROW_RESERVE: Final = _env_int("XLSX_BUNDLE_MIN_ROW_RESERVE", 200)
# 병합 제목 행이 모든 컬럼 헤더에 반복 병합된 시트(예: epi 계열, 한 문장이 26개 헤더에 중복)를
# 위한 공통 접두사 호이스팅 최소 길이. 이보다 짧은 공통 접두사(예: "채널 / ")는 의미 있는
# 계층 구조일 수 있어 건드리지 않는다.
HEADER_COMMON_PREFIX_MIN_CHARS: Final = _env_int("XLSX_HEADER_COMMON_PREFIX_MIN_CHARS", 24)
# 묶음(위치 기반) 형식은 표본 행에서 기존 헤더-값 쌍 형식보다 실제 문자 수가 10% 이상 줄어들
# 때만 채택한다. 희소 행(빈 칸이 많아 쌍 형식이 이미 짧은 시트)이나 헤더보다 데이터 열이 훨씬
# 넓은 시트에서 위치 기반 행이 오히려 길어지는 역행을 막는다.
BUNDLE_SAMPLE_ROWS: Final = _env_int("XLSX_BUNDLE_SAMPLE_ROWS", 50)
BUNDLE_BENEFIT_RATIO: Final = 0.9
STREAMING_SHEET_XML_BYTES_THRESHOLD: Final = 50 * 1024 * 1024
STREAMING_USED_CELL_THRESHOLD: Final = 500_000
STREAMING_MERGED_RANGE_LIMIT: Final = 0
MergeRange = tuple[int, int, int, int]
RowFallback = Callable[[], list[str]]
MetricGroup = tuple[str, list[tuple[int, str]]]
WideMetricLayout = tuple[list[int], list[MetricGroup]]
StyleFormats = dict[int, str]

LOGGER = logging.getLogger(__name__)

DATAMONITOR_MARKER_SHEETS: Final[frozenset[str]] = frozenset(
    {
        "setup_calc",
        "p_input",
        "input_c",
        "input_dm",
        "pivot_product",
    }
)
DATAMONITOR_SKIP_NAMES: Final[frozenset[str]] = frozenset(
    {
        "home",
        "instructions",
        "blank",
        "my worksheet",
    }
)
DATAMONITOR_RAW_INPUT_NAMES: Final[frozenset[str]] = frozenset(
    {
        "p_input",
        "input_c",
        "input_dm",
    }
)
DATAMONITOR_CALC_TOKENS: Final[tuple[str, ...]] = ("calc", "engine")
DATAMONITOR_LARGE_ROW_LIMIT: Final = 50_000
# 시트 스킵 고지 문구: 조용한 누락을 없애기 위해 스킵된 시트명과 사유를 사용자 응답 notes로 노출한다.
SHEET_SKIP_NOTE_PREFIX: Final = "xlsx 시트 스킵 고지"
SHEET_SKIP_REASON_LABELS: Final[dict[str, str]] = {
    "empty_or_pivot_cache": "빈 시트 또는 피벗 캐시",
    "navigation_or_blank_sheet": "탐색/빈 안내 시트",
    "pivot_sheet": "피벗 시트",
    "large_raw_input_sheet": "대형 원천 입력 시트",
    "setup_or_backsheet": "설정/백시트",
    "large_raw_data_sheet": "대형 원천 데이터 시트",
    "calculation_sheet": "계산(calc) 시트",
}
CROSSTABLE_LINK_RE: Final = re.compile(r'#Table!A(?P<row>\d+)')
CROSSTABLE_MIN_INDEX_LINKS: Final = 10

BUILTIN_NUM_FORMATS: Final[dict[int, str]] = {
    9: "0%",
    10: "0.00%",
    14: "m/d/yy",
    15: "d-mmm-yy",
    16: "d-mmm",
    17: "mmm-yy",
    18: "h:mm AM/PM",
    19: "h:mm:ss AM/PM",
    20: "h:mm",
    21: "h:mm:ss",
    22: "m/d/yy h:mm",
    37: "#,##0 ;(#,##0)",
    38: "#,##0 ;[Red](#,##0)",
    39: "#,##0.00;(#,##0.00)",
    40: "#,##0.00;[Red](#,##0.00)",
    44: '_($* #,##0.00_);_($* (#,##0.00);_($* "-"??_);_(@_)',
}


class XlsxPreprocessError(RuntimeError):
    """Raised when an XLSX workbook cannot be converted into searchable chunks."""


@dataclass(frozen=True, slots=True)
class SheetSkip:
    """A sheet excluded from indexing, kept for explicit user notification."""

    sheet_name: str
    reason: str

    def note(self) -> str:
        label = SHEET_SKIP_REASON_LABELS.get(self.reason, self.reason)
        return (
            f"{SHEET_SKIP_NOTE_PREFIX}: 시트 '{self.sheet_name}'은(는) "
            f"{label}({self.reason}) 사유로 색인에서 제외되었습니다."
        )


@dataclass(frozen=True, slots=True)
class SheetFeatures:
    """Lightweight worksheet features used for conservative Datamonitor sheet filtering."""

    row_count: int
    column_count: int
    used_cell_count: int
    formula_cell_count: int
    merged_range_count: int

    @property
    def formula_ratio(self) -> float:
        return self.formula_cell_count / self.used_cell_count if self.used_cell_count else 0.0

    @property
    def is_empty(self) -> bool:
        return self.used_cell_count == 0


@dataclass(frozen=True, slots=True)
class SheetRow:
    row_number: int
    values: list[str]


@dataclass(frozen=True, slots=True)
class CrosstableStart:
    table_number: int
    start_row: int
    title: str
    base: str
    sample_size: str


@dataclass(frozen=True, slots=True)
class WeeklyCanvasPanel:
    label: str
    title_column: int
    start_column: int
    end_column: int


@dataclass(frozen=True, slots=True)
class WeeklyCanvasTable:
    title: str
    start_row: int
    end_row: int
    data_start_index: int
    row_indexes: list[int]


WEEKLY_CANVAS_PANELS: Final[tuple[WeeklyCanvasPanel, ...]] = (
    WeeklyCanvasPanel("원본", 2, 3, 18),
    WeeklyCanvasPanel("설연휴 보정", 20, 21, 36),
)
WEEKLY_CANVAS_TITLE_TOKENS: Final[tuple[str, ...]] = ("주간 도매", "Sell out Trend")


def extract_xlsx_chunks(
    path: Path,
    *,
    chunk_char_limit: int = DEFAULT_CHUNK_CHAR_LIMIT,
    skip_report: list[SheetSkip] | None = None,
) -> list[str]:
    """Return header-value-preserving chunks from an XLSX workbook.

    skip_report가 주어지면 필터로 제외된 시트(SheetSkip)를 담아 호출자가
    사용자 응답 notes로 고지할 수 있게 한다. 스킵 판정 로직 자체는 바꾸지 않는다.
    """
    if chunk_char_limit < 80:
        raise XlsxPreprocessError("chunk_char_limit must be at least 80")
    try:
        with ZipFile(path) as archive:
            shared_strings = _shared_strings(archive)
            style_formats = _style_formats(archive)
            sheet_paths = _sheet_paths(archive)
            sheet_path_by_name = {sheet_name: sheet_path for sheet_name, sheet_path in sheet_paths}
            crosstable_starts = _extract_table_starts_from_index(
                archive.read(sheet_path_by_name["INDEX"]),
                shared_strings,
                style_formats,
            ) if "INDEX" in sheet_path_by_name else []
            apply_crosstable_split = len(crosstable_starts) >= CROSSTABLE_MIN_INDEX_LINKS
            apply_sheet_filter = _is_datamonitor_model_workbook(sheet_name for sheet_name, _path in sheet_paths)
            chunks: list[str] = []
            for sheet_name, sheet_path in sheet_paths:
                sheet_xml = archive.read(sheet_path)
                if apply_crosstable_split and sheet_name in {"Table", "FREQ"}:
                    rows_with_numbers = _sheet_rows_with_numbers(sheet_xml, shared_strings, style_formats)
                    chunks.extend(
                        _crosstable_chunks_for_sheet(
                            sheet_name,
                            rows_with_numbers,
                            crosstable_starts,
                            chunk_char_limit,
                        )
                    )
                    continue
                weekly_rows = _sheet_rows_with_numbers(sheet_xml, shared_strings, style_formats)
                weekly_chunks = _weekly_canvas_chunks_for_sheet(sheet_name, weekly_rows, chunk_char_limit)
                if weekly_chunks:
                    chunks.extend(weekly_chunks)
                    continue
                if apply_sheet_filter:
                    features = _sheet_features(sheet_xml)
                    skip_reason = _datamonitor_skip_reason(sheet_name, features)
                    if skip_reason:
                        if skip_report is not None:
                            skip_report.append(SheetSkip(sheet_name=sheet_name, reason=skip_reason))
                        LOGGER.info(
                            "xlsx sheet skipped: sheet=%s reason=%s rows=%s cols=%s used=%s formulas=%s merged=%s",
                            sheet_name,
                            skip_reason,
                            features.row_count,
                            features.column_count,
                            features.used_cell_count,
                            features.formula_cell_count,
                            features.merged_range_count,
                        )
                        continue
                    LOGGER.info(
                        "xlsx sheet kept: sheet=%s rows=%s cols=%s used=%s formulas=%s merged=%s",
                        sheet_name,
                        features.row_count,
                        features.column_count,
                        features.used_cell_count,
                        features.formula_cell_count,
                        features.merged_range_count,
                    )
                rows = _sheet_rows(sheet_xml, shared_strings, style_formats)
                chunks.extend(_chunks_for_sheet(sheet_name, rows, chunk_char_limit))
    except (BadZipFile, ElementTree.ParseError, KeyError, OSError) as exc:
        raise XlsxPreprocessError(f"xlsx preprocessing failed: {exc}") from exc
    if not chunks:
        raise XlsxPreprocessError("xlsx preprocessing produced no chunks")
    return chunks


def should_stream_xlsx_chunks(path: Path) -> bool:
    """Return whether the workbook should use the large flat-sheet streaming path."""
    try:
        with ZipFile(path) as archive:
            if "xl/sharedStrings.xml" in archive.namelist():
                return False
            sheet_paths = _sheet_paths(archive)
            if _is_datamonitor_model_workbook(sheet_name for sheet_name, _path in sheet_paths):
                return False
            for _sheet_name, sheet_path in sheet_paths:
                info = archive.getinfo(sheet_path)
                features = _sheet_features_streaming(archive, sheet_path)
                if not _is_streaming_sheet_candidate(info.file_size, features):
                    return False
            return bool(sheet_paths)
    except (BadZipFile, ElementTree.ParseError, KeyError, OSError):
        return False


def iter_xlsx_chunks(
    path: Path,
    *,
    chunk_char_limit: int = DEFAULT_CHUNK_CHAR_LIMIT,
) -> Iterator[str]:
    """Yield header-value-preserving chunks from a large flat XLSX workbook."""
    if chunk_char_limit < 80:
        raise XlsxPreprocessError("chunk_char_limit must be at least 80")
    produced = False
    try:
        with ZipFile(path) as archive:
            shared_strings = _shared_strings(archive)
            style_formats = _style_formats(archive)
            sheet_paths = _sheet_paths(archive)
            for sheet_name, sheet_path in sheet_paths:
                for chunk in iter_chunks_for_sheet_streaming(
                    sheet_name,
                    iter_sheet_rows_streaming(archive, sheet_path, shared_strings, style_formats),
                    chunk_char_limit,
                ):
                    produced = True
                    yield chunk
    except (BadZipFile, ElementTree.ParseError, KeyError, OSError) as exc:
        raise XlsxPreprocessError(f"xlsx streaming preprocessing failed: {exc}") from exc
    if not produced:
        raise XlsxPreprocessError("xlsx streaming preprocessing produced no chunks")


def iter_sheet_rows_streaming(
    zip_file: ZipFile,
    sheet_path: str,
    shared_strings: list[str],
    style_formats: StyleFormats,
) -> Iterator[list[str]]:
    """Yield non-empty worksheet rows without materializing the sheet XML DOM."""
    with zip_file.open(sheet_path) as sheet_file:
        for event, element in ElementTree.iterparse(sheet_file, events=("end",)):
            if event == "end" and element.tag == f"{SHEET_NS}row":
                values = [
                    _cell_value(cell, shared_strings, style_formats)
                    for cell in element.findall(f"{SHEET_NS}c")
                ]
                if any(value.strip() for value in values):
                    yield values
                element.clear()


def iter_chunks_for_sheet_streaming(
    sheet_name: str,
    row_iter: Iterator[list[str]],
    chunk_char_limit: int,
) -> Iterator[str]:
    """Yield chunks with the same row and wide-metric logic as `_chunks_for_sheet`."""
    first_rows: list[list[str]] = []
    for _index in range(10):
        try:
            first_rows.append(next(row_iter))
        except StopIteration:
            break
    header = _first_header(first_rows)
    if header is None:
        return
    header_index, headers = header
    wide_layout = _wide_metric_layout(headers)
    data_rows = _iter_rows_after_header(first_rows, row_iter, header_index)
    first_row_number = header_index + 2
    if wide_layout is not None:
        yield from _iter_wide_metric_chunks(
            sheet_name,
            data_rows,
            first_row_number,
            headers,
            wide_layout,
            chunk_char_limit,
        )
        return
    yield from _iter_standard_chunks(
        sheet_name,
        data_rows,
        first_row_number,
        headers,
        chunk_char_limit,
    )


def _is_streaming_sheet_candidate(sheet_xml_bytes: int, features: SheetFeatures) -> bool:
    is_large = (
        sheet_xml_bytes >= STREAMING_SHEET_XML_BYTES_THRESHOLD
        or features.used_cell_count >= STREAMING_USED_CELL_THRESHOLD
    )
    return is_large and features.merged_range_count <= STREAMING_MERGED_RANGE_LIMIT


def _sheet_features_streaming(archive: ZipFile, sheet_path: str) -> SheetFeatures:
    row_count = 0
    column_count = 0
    used_cell_count = 0
    formula_cell_count = 0
    merged_range_count = 0
    with archive.open(sheet_path) as sheet_file:
        for event, element in ElementTree.iterparse(sheet_file, events=("end",)):
            if event == "end" and element.tag == f"{SHEET_NS}row":
                row_count = max(row_count, _row_number(element, row_count + 1))
                for fallback_column, cell in enumerate(element.findall(f"{SHEET_NS}c"), start=1):
                    cell_row, cell_column = _cell_position(
                        str(cell.attrib.get("r") or ""),
                        row_count,
                        fallback_column,
                    )
                    row_count = max(row_count, cell_row)
                    column_count = max(column_count, cell_column)
                    has_formula = cell.find(f"{SHEET_NS}f") is not None
                    has_value = (cell.findtext(f"{SHEET_NS}v") or "").strip() != ""
                    has_inline_text = cell.find(f"{SHEET_NS}is") is not None
                    if has_formula or has_value or has_inline_text:
                        used_cell_count += 1
                    if has_formula:
                        formula_cell_count += 1
                element.clear()
            elif event == "end" and element.tag == f"{SHEET_NS}mergeCell":
                if str(element.attrib.get("ref") or ""):
                    merged_range_count += 1
                element.clear()
    return SheetFeatures(
        row_count=row_count,
        column_count=column_count,
        used_cell_count=used_cell_count,
        formula_cell_count=formula_cell_count,
        merged_range_count=merged_range_count,
    )


def _iter_rows_after_header(
    first_rows: list[list[str]],
    row_iter: Iterator[list[str]],
    header_index: int,
) -> Iterator[list[str]]:
    yield from first_rows[header_index + 1 :]
    yield from row_iter


def _is_datamonitor_model_workbook(sheet_names: Iterable[str]) -> bool:
    normalized = {str(name).strip().lower() for name in sheet_names}
    return len(normalized & DATAMONITOR_MARKER_SHEETS) >= 3


def _sheet_features(sheet_xml: bytes) -> SheetFeatures:
    root = ElementTree.fromstring(sheet_xml)
    row_count = 0
    column_count = 0
    used_cell_count = 0
    formula_cell_count = 0
    merged_range_count = 0
    for row in root.findall(f".//{SHEET_NS}row"):
        row_count = max(row_count, _row_number(row, row_count + 1))
        for fallback_column, cell in enumerate(row.findall(f"{SHEET_NS}c"), start=1):
            cell_row, cell_column = _cell_position(
                str(cell.attrib.get("r") or ""),
                row_count,
                fallback_column,
            )
            row_count = max(row_count, cell_row)
            column_count = max(column_count, cell_column)
            has_formula = cell.find(f"{SHEET_NS}f") is not None
            has_value = (cell.findtext(f"{SHEET_NS}v") or "").strip() != ""
            has_inline_text = cell.find(f"{SHEET_NS}is") is not None
            if has_formula or has_value or has_inline_text:
                used_cell_count += 1
            if has_formula:
                formula_cell_count += 1
    for merge_cell in root.findall(f".//{SHEET_NS}mergeCell"):
        if str(merge_cell.attrib.get("ref") or ""):
            merged_range_count += 1
    return SheetFeatures(
        row_count=row_count,
        column_count=column_count,
        used_cell_count=used_cell_count,
        formula_cell_count=formula_cell_count,
        merged_range_count=merged_range_count,
    )


def _datamonitor_skip_reason(sheet_name: str, features: SheetFeatures) -> str:
    normalized = sheet_name.strip().lower()
    if features.is_empty:
        return "empty_or_pivot_cache"
    if normalized in DATAMONITOR_SKIP_NAMES:
        return "navigation_or_blank_sheet"
    if "pivot" in normalized:
        return "pivot_sheet"
    if normalized in DATAMONITOR_RAW_INPUT_NAMES:
        return "large_raw_input_sheet"
    if "backsheet" in normalized or normalized.startswith("setup"):
        return "setup_or_backsheet"
    if normalized == "data" and features.row_count > DATAMONITOR_LARGE_ROW_LIMIT:
        return "large_raw_data_sheet"
    if features.row_count > DATAMONITOR_LARGE_ROW_LIMIT:
        return "large_raw_input_sheet"
    if any(token in normalized for token in DATAMONITOR_CALC_TOKENS):
        return "calculation_sheet"
    if normalized == "model" and features.formula_ratio >= 0.80:
        return "calculation_sheet"
    return ""


def _shared_strings(archive: ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    values: list[str] = []
    for item in root.findall(f"{SHEET_NS}si"):
        parts = [node.text or "" for node in item.iter(f"{SHEET_NS}t")]
        values.append("".join(parts).strip())
    return values


def _style_formats(archive: ZipFile) -> StyleFormats:
    if "xl/styles.xml" not in archive.namelist():
        return {}
    root = ElementTree.fromstring(archive.read("xl/styles.xml"))
    custom_formats = {
        int(node.attrib["numFmtId"]): str(node.attrib.get("formatCode") or "")
        for node in root.findall(f".//{SHEET_NS}numFmt")
        if str(node.attrib.get("numFmtId") or "").isdigit()
    }
    formats: StyleFormats = {}
    cell_xfs = root.find(f"{SHEET_NS}cellXfs")
    if cell_xfs is None:
        return formats
    for index, xf in enumerate(cell_xfs.findall(f"{SHEET_NS}xf")):
        raw_num_fmt_id = str(xf.attrib.get("numFmtId") or "")
        if not raw_num_fmt_id.isdigit():
            continue
        num_fmt_id = int(raw_num_fmt_id)
        format_code = custom_formats.get(num_fmt_id) or BUILTIN_NUM_FORMATS.get(num_fmt_id)
        if format_code:
            formats[index] = format_code
    return formats


def _sheet_paths(archive: ZipFile) -> list[tuple[str, str]]:
    workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    rels = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    rel_targets = {
        str(rel.attrib.get("Id") or ""): _normalize_sheet_target(str(rel.attrib.get("Target") or ""))
        for rel in rels.findall(f"{REL_NS}Relationship")
    }
    sheets: list[tuple[str, str]] = []
    for sheet in workbook.findall(f".//{SHEET_NS}sheet"):
        rel_id = str(sheet.attrib.get(f"{OFFICE_REL_NS}id") or "")
        target = rel_targets.get(rel_id)
        if target:
            sheets.append((str(sheet.attrib.get("name") or rel_id), target))
    return sheets


def _normalize_sheet_target(target: str) -> str:
    if target.startswith("/"):
        return target.lstrip("/")
    if target.startswith("xl/"):
        return target
    return f"xl/{target}"


def _extract_table_starts_from_index(
    sheet_xml: bytes,
    shared_strings: list[str],
    style_formats: StyleFormats,
) -> list[CrosstableStart]:
    root = ElementTree.fromstring(sheet_xml)
    starts: list[CrosstableStart] = []
    seen_rows: set[int] = set()
    fallback_row_number = 0
    for row in root.findall(f".//{SHEET_NS}row"):
        fallback_row_number += 1
        row_number = _row_number(row, fallback_row_number)
        values: list[str] = []
        start_row: int | None = None
        for fallback_column, cell in enumerate(row.findall(f"{SHEET_NS}c"), start=1):
            _cell_row, column_number = _cell_position(
                str(cell.attrib.get("r") or ""),
                row_number,
                fallback_column,
            )
            _set_row_value(values, column_number, _cell_value(cell, shared_strings, style_formats))
            formula = cell.findtext(f"{SHEET_NS}f") or ""
            match = CROSSTABLE_LINK_RE.search(formula)
            if match is not None:
                start_row = int(match.group("row"))
        if start_row is None or start_row in seen_rows:
            continue
        seen_rows.add(start_row)
        starts.append(
            CrosstableStart(
                table_number=len(starts) + 1,
                start_row=start_row,
                title=_clean_value(values[0]) if values else "",
                base=_normalize_index_base(values[1]) if len(values) > 1 else "",
                sample_size=_clean_value(values[2]) if len(values) > 2 else "",
            )
        )
    return sorted(starts, key=lambda item: item.start_row)


def _normalize_index_base(value: str) -> str:
    cleaned = _clean_value(value)
    return cleaned.split(":", 1)[1].strip() if cleaned.lower().startswith("base:") else cleaned


def _sheet_rows(
    sheet_xml: bytes,
    shared_strings: list[str],
    style_formats: StyleFormats,
) -> list[list[str]]:
    root = ElementTree.fromstring(sheet_xml)
    merge_ranges = _merge_ranges(root) if b"mergeCell" in sheet_xml else []
    if merge_ranges:
        return _sheet_rows_with_merge_fill(root, shared_strings, style_formats, merge_ranges)
    rows: list[list[str]] = []
    for row in root.findall(f".//{SHEET_NS}row"):
        values: list[str] = []
        for cell in row.findall(f"{SHEET_NS}c"):
            values.append(_cell_value(cell, shared_strings, style_formats))
        if any(value.strip() for value in values):
            rows.append(values)
    return rows


def _sheet_rows_with_numbers(
    sheet_xml: bytes,
    shared_strings: list[str],
    style_formats: StyleFormats,
) -> list[SheetRow]:
    root = ElementTree.fromstring(sheet_xml)
    merge_ranges = _merge_ranges(root) if b"mergeCell" in sheet_xml else []
    if merge_ranges:
        return _sheet_rows_with_merge_fill_and_numbers(root, shared_strings, style_formats, merge_ranges)
    rows: list[SheetRow] = []
    fallback_row_number = 0
    for row in root.findall(f".//{SHEET_NS}row"):
        fallback_row_number += 1
        row_number = _row_number(row, fallback_row_number)
        values: list[str] = []
        for fallback_column, cell in enumerate(row.findall(f"{SHEET_NS}c"), start=1):
            _cell_row, column_number = _cell_position(
                str(cell.attrib.get("r") or ""),
                row_number,
                fallback_column,
            )
            _set_row_value(values, column_number, _cell_value(cell, shared_strings, style_formats))
        if any(value.strip() for value in values):
            rows.append(SheetRow(row_number=row_number, values=values))
    return rows


def _sheet_rows_with_merge_fill_and_numbers(
    root: ElementTree.Element,
    shared_strings: list[str],
    style_formats: StyleFormats,
    merge_ranges: list[MergeRange],
) -> list[SheetRow]:
    starts: dict[int, list[MergeRange]] = {}
    for merge_range in merge_ranges:
        starts.setdefault(merge_range[0], []).append(merge_range)

    rows: list[SheetRow] = []
    active: list[MergeRange] = []
    anchor_values: dict[MergeRange, str] = {}
    fallback_row_number = 0
    for row in root.findall(f".//{SHEET_NS}row"):
        fallback_row_number += 1
        row_number = _row_number(row, fallback_row_number)
        active = [item for item in active if item[2] >= row_number]
        active.extend(starts.get(row_number, []))

        values: list[str] = []
        for fallback_column, cell in enumerate(row.findall(f"{SHEET_NS}c"), start=1):
            _cell_row, column_number = _cell_position(
                str(cell.attrib.get("r") or ""),
                row_number,
                fallback_column,
            )
            _set_row_value(values, column_number, _cell_value(cell, shared_strings, style_formats))

        for merge_range in active:
            r1, c1, r2, c2 = merge_range
            if row_number == r1:
                anchor_values[merge_range] = values[c1 - 1] if c1 <= len(values) else ""
            anchor = anchor_values.get(merge_range, "")
            if not anchor or not (r1 <= row_number <= r2):
                continue
            for column_number in range(c1, c2 + 1):
                if column_number > len(values) or not values[column_number - 1].strip():
                    _set_row_value(values, column_number, anchor)

        if any(value.strip() for value in values):
            rows.append(SheetRow(row_number=row_number, values=values))
    return rows


def _sheet_rows_with_merge_fill(
    root: ElementTree.Element,
    shared_strings: list[str],
    style_formats: StyleFormats,
    merge_ranges: list[MergeRange],
) -> list[list[str]]:
    starts: dict[int, list[MergeRange]] = {}
    for merge_range in merge_ranges:
        starts.setdefault(merge_range[0], []).append(merge_range)

    rows: list[list[str]] = []
    active: list[MergeRange] = []
    anchor_values: dict[MergeRange, str] = {}
    fallback_row_number = 0
    for row in root.findall(f".//{SHEET_NS}row"):
        fallback_row_number += 1
        row_number = _row_number(row, fallback_row_number)
        active = [item for item in active if item[2] >= row_number]
        active.extend(starts.get(row_number, []))

        values: list[str] = []
        for fallback_column, cell in enumerate(row.findall(f"{SHEET_NS}c"), start=1):
            _cell_row, column_number = _cell_position(
                str(cell.attrib.get("r") or ""),
                row_number,
                fallback_column,
            )
            _set_row_value(values, column_number, _cell_value(cell, shared_strings, style_formats))

        for merge_range in active:
            r1, c1, r2, c2 = merge_range
            if row_number == r1:
                anchor_values[merge_range] = values[c1 - 1] if c1 <= len(values) else ""
            anchor = anchor_values.get(merge_range, "")
            if not anchor or not (r1 <= row_number <= r2):
                continue
            for column_number in range(c1, c2 + 1):
                if column_number > len(values) or not values[column_number - 1].strip():
                    _set_row_value(values, column_number, anchor)

        if any(value.strip() for value in values):
            rows.append(values)
    return rows


def _merge_ranges(root: ElementTree.Element) -> list[MergeRange]:
    ranges: list[MergeRange] = []
    for node in root.findall(f".//{SHEET_NS}mergeCell"):
        parsed = _cell_range(str(node.attrib.get("ref") or ""))
        if parsed is None:
            continue
        r1, c1, r2, c2 = parsed
        if r1 != r2 or c1 != c2:
            ranges.append(parsed)
    return sorted(ranges)


def _cell_range(value: str) -> MergeRange | None:
    parts = value.split(":", 1)
    if len(parts) != 2:
        return None
    start = _cell_position(parts[0], 0, 0)
    end = _cell_position(parts[1], 0, 0)
    return start[0], start[1], end[0], end[1]


def _cell_position(value: str, fallback_row: int, fallback_column: int) -> tuple[int, int]:
    column = 0
    row_digits: list[str] = []
    for char in value:
        if "A" <= char <= "Z":
            column = column * 26 + ord(char) - 64
        elif char.isdigit():
            row_digits.append(char)
    row = int("".join(row_digits)) if row_digits else fallback_row
    return row, column or fallback_column


def _row_number(row: ElementTree.Element, fallback: int) -> int:
    raw = str(row.attrib.get("r") or "")
    return int(raw) if raw.isdigit() else fallback


def _set_row_value(values: list[str], column_number: int, value: str) -> None:
    if column_number < 1:
        values.append(value)
        return
    while len(values) < column_number:
        values.append("")
    values[column_number - 1] = value


def _cell_value(cell: ElementTree.Element, shared_strings: list[str], style_formats: StyleFormats) -> str:
    cell_type = cell.attrib.get("t")
    match cell_type:
        case "inlineStr":
            text = "".join(node.text or "" for node in cell.iter(f"{SHEET_NS}t"))
            return _clean_cell_text(text)
        case "s":
            raw_index = cell.findtext(f"{SHEET_NS}v") or ""
            index = int(raw_index) if raw_index.isdigit() else -1
            if 0 <= index < len(shared_strings):
                return _clean_cell_text(shared_strings[index])
            return ""
        case "b":
            value = cell.findtext(f"{SHEET_NS}v") or ""
            return "TRUE" if value == "1" else "FALSE"
        case "str" | "e" | None:
            return _format_or_clean_cell_value(cell, style_formats)
        case _:
            return _format_or_clean_cell_value(cell, style_formats)


def _format_or_clean_cell_value(cell: ElementTree.Element, style_formats: StyleFormats) -> str:
    raw_value = cell.findtext(f"{SHEET_NS}v") or ""
    format_code = _cell_format_code(cell, style_formats)
    if format_code:
        formatted = _format_number(raw_value, format_code)
        if formatted:
            return formatted
    return _clean_value(raw_value)


def _cell_format_code(cell: ElementTree.Element, style_formats: StyleFormats) -> str:
    raw_style = str(cell.attrib.get("s") or "")
    if not raw_style.isdigit():
        return ""
    return style_formats.get(int(raw_style), "")


def _format_number(raw_value: str, format_code: str) -> str:
    try:
        value = Decimal(raw_value)
    except InvalidOperation:
        return ""
    normalized_code = _normalized_format_code(format_code)
    if _is_date_format(normalized_code):
        return _format_excel_date(value)
    if "%" in normalized_code:
        return _format_percent(value, normalized_code)
    if "$" in format_code:
        return _format_currency(value, format_code)
    return ""


def _normalized_format_code(format_code: str) -> str:
    first_section = format_code.split(";", 1)[0]
    cleaned: list[str] = []
    in_quote = False
    in_bracket = False
    skip_next = False
    for char in first_section:
        if skip_next:
            skip_next = False
            continue
        if char == "\\":
            skip_next = True
            continue
        if char == '"':
            in_quote = not in_quote
            continue
        if char == "[":
            in_bracket = True
            continue
        if char == "]":
            in_bracket = False
            continue
        if not in_quote and not in_bracket and char not in "_*":
            cleaned.append(char.lower())
    return "".join(cleaned)


def _is_date_format(normalized_code: str) -> bool:
    if "%" in normalized_code:
        return False
    return any(char in normalized_code for char in ("y", "d")) and "m" in normalized_code


def _format_excel_date(value: Decimal) -> str:
    days = int(value.to_integral_value(rounding=ROUND_HALF_UP))
    return (datetime(1899, 12, 30) + timedelta(days=days)).date().isoformat()


def _format_percent(value: Decimal, normalized_code: str) -> str:
    decimals = _decimal_places(normalized_code)
    percent = value * Decimal(100)
    return f"{_format_decimal(percent, decimals)}%"


def _format_currency(value: Decimal, format_code: str) -> str:
    decimals = _decimal_places(format_code)
    return f"${_format_decimal(value, decimals, grouped=True)}"


def _decimal_places(format_code: str) -> int:
    first_section = format_code.split(";", 1)[0]
    if "." not in first_section:
        return 0
    decimal_part = first_section.split(".", 1)[1]
    return sum(char in "0#?" for char in decimal_part)


def _format_decimal(value: Decimal, decimals: int, *, grouped: bool = False) -> str:
    quantizer = Decimal(1) if decimals <= 0 else Decimal(1).scaleb(-decimals)
    rounded = value.quantize(quantizer, rounding=ROUND_HALF_UP)
    spec = f",.{decimals}f" if grouped else f".{decimals}f"
    text = format(rounded, spec)
    if decimals > 0:
        text = text.rstrip("0").rstrip(".")
    return text


def _iter_packed_lines(lines: Iterable[str], chunk_char_limit: int) -> Iterator[str]:
    """Pack already-final lines into newline-joined chunks within the char limit."""
    current = ""
    for line in lines:
        if not line:
            continue
        if len(line) > chunk_char_limit:
            if current:
                yield current
                current = ""
            yield from _split_long_line(line, chunk_char_limit)
            continue
        if current and len(current) + 1 + len(line) > chunk_char_limit:
            yield current
            current = line
        else:
            current = line if not current else f"{current}\n{line}"
    if current:
        yield current


def _bundle_row_line(row_number: int, values: list[str]) -> str:
    """Positional value row for header-once bundles; trailing empties trimmed."""
    cleaned = [_clean_value(value) for value in values]
    while cleaned and not cleaned[-1]:
        cleaned.pop()
    if not any(cleaned):
        return ""
    return f"행 {row_number}: " + " | ".join(cleaned)


def _iter_row_bundles(
    context: str,
    row_entries: Iterable[tuple[str, "RowFallback"]],
    chunk_char_limit: int,
) -> Iterator[str]:
    """Yield chunks of `context + N rows`; overlong rows fall back to legacy pair lines.

    row_entries: (bundle_line, fallback). bundle_line is the positional row line;
    fallback() lazily builds self-describing header-value pair lines (already split
    within the char limit) used when the positional row alone cannot fit the row budget.
    """
    row_budget = chunk_char_limit - len(context) - 1
    body: list[str] = []
    body_chars = 0
    for bundle_line, fallback in row_entries:
        if not bundle_line or len(bundle_line) > row_budget:
            fallback_lines = fallback()
            if not fallback_lines:
                continue
            if body:
                yield context + "\n" + "\n".join(body)
                body = []
                body_chars = 0
            yield from _iter_packed_lines(fallback_lines, chunk_char_limit)
            continue
        added = len(bundle_line) + (1 if body else 0)
        if body and (body_chars + added > row_budget or len(body) >= XLSX_BUNDLE_MAX_ROWS):
            yield context + "\n" + "\n".join(body)
            body = []
            body_chars = 0
            added = len(bundle_line)
        body.append(bundle_line)
        body_chars += added
    if body:
        yield context + "\n" + "\n".join(body)


def _standard_fallback_lines(
    sheet_name: str,
    row_number: int,
    headers: list[str],
    row: list[str],
    chunk_char_limit: int,
) -> list[str]:
    line = _row_line(sheet_name, row_number, headers, row)
    if not line:
        return []
    if len(line) > chunk_char_limit:
        return _split_long_line(line, chunk_char_limit)
    return [line]


def _hoist_common_header_prefix(label: str, headers: list[str]) -> tuple[str, list[str]]:
    """Deduplicate a long prefix shared by most headers into the chunk context label.

    제목 행이 컬럼 헤더에 반복 병합된 시트에서 같은 문장이 행마다 수십 번 반복되는 것을
    막는다. 접두사는 라벨(`표머리공통`)로 옮겨져 모든 청크에 남으므로 정보 손실이 없다.
    전체 일치 대신 다수결 버킷을 쓰는 이유: 헤더 감지가 빈 칸을 `컬럼N`으로 채우고 고정
    컬럼(국가·성별 등)은 접두사를 공유하지 않아, 전체 공통 접두사는 사실상 항상 비기 때문.
    """
    candidates = [header for header in headers if len(header) >= HEADER_COMMON_PREFIX_MIN_CHARS]
    if len(candidates) < 3 or len(candidates) * 2 < len([h for h in headers if h]):
        return label, headers
    buckets: dict[str, list[str]] = {}
    for header in candidates:
        buckets.setdefault(header[:HEADER_COMMON_PREFIX_MIN_CHARS], []).append(header)
    bucket = max(buckets.values(), key=len)
    if len(bucket) < 3 or len(bucket) * 2 < len(candidates):
        return label, headers
    prefix = bucket[0]
    for header in bucket[1:]:
        limit = min(len(prefix), len(header))
        index = 0
        while index < limit and prefix[index] == header[index]:
            index += 1
        prefix = prefix[:index]
    boundary = max(prefix.rfind(" "), prefix.rfind("/"), prefix.rfind(":"), prefix.rfind("\n"))
    if boundary + 1 < HEADER_COMMON_PREFIX_MIN_CHARS:
        return label, headers
    cut = prefix[: boundary + 1]
    common = _clean_value(cut)
    stripped = [
        header[len(cut) :].strip() or header if header.startswith(cut) else header
        for header in headers
    ]
    return f"{label} | 표머리공통: {common}", _dedupe_headers(stripped)


def _standard_bundle_context(sheet_name: str, headers: list[str], chunk_char_limit: int) -> str | None:
    context = f"시트: {sheet_name} | 표머리: " + " | ".join(headers)
    if len(context) > chunk_char_limit - BUNDLE_MIN_ROW_RESERVE:
        return None
    return context


def _bundle_saves_chars(
    sample: list[tuple[str, str]],
    context_len: int,
    chunk_char_limit: int,
) -> bool:
    """Return whether positional bundles beat legacy pair lines on the sampled rows."""
    bundle_chars = 0
    pair_chars = 0
    row_budget = chunk_char_limit - context_len - 1
    for bundle_line, pair_line in sample:
        pair_chars += len(pair_line) + 1
        if bundle_line and len(bundle_line) <= row_budget:
            bundle_chars += len(bundle_line) + 1
        else:
            bundle_chars += len(pair_line) + 1
    if not pair_chars:
        return True
    estimated_chunks = max(1, -(-bundle_chars // max(row_budget, 1)))
    bundle_total = bundle_chars + estimated_chunks * (context_len + 1)
    return bundle_total <= pair_chars * BUNDLE_BENEFIT_RATIO


def _iter_bundled_standard_chunks(
    sheet_name: str,
    data_rows: Iterable[list[str]],
    first_row_number: int,
    headers: list[str],
    chunk_char_limit: int,
) -> Iterator[str]:
    sheet_name, headers = _hoist_common_header_prefix(sheet_name, headers)
    context = _standard_bundle_context(sheet_name, headers, chunk_char_limit)
    row_iter = iter(data_rows)
    buffered: list[list[str]] = []
    if context is not None:
        for row in row_iter:
            buffered.append(row)
            if len(buffered) >= BUNDLE_SAMPLE_ROWS:
                break
        sample = [
            (
                _bundle_row_line(first_row_number + offset, row),
                _row_line(sheet_name, first_row_number + offset, headers, row),
            )
            for offset, row in enumerate(buffered)
        ]
        if not _bundle_saves_chars(sample, len(context), chunk_char_limit):
            context = None
    all_rows = chain(buffered, row_iter)
    if context is None:
        # 묶음 이득이 없거나(희소 행) 문맥이 청크 예산을 넘는(초광폭 표머리) 시트는
        # 기존 행별 헤더-값 쌍 형식을 유지한다(잘림 방지 경로).
        yield from _iter_packed_lines(
            (
                line
                for row_number, row in enumerate(all_rows, start=first_row_number)
                for line in _standard_fallback_lines(sheet_name, row_number, headers, row, chunk_char_limit)
            ),
            chunk_char_limit,
        )
        return
    row_entries = (
        (
            _bundle_row_line(row_number, row),
            lambda number=row_number, values=row: _standard_fallback_lines(
                sheet_name, number, headers, values, chunk_char_limit
            ),
        )
        for row_number, row in enumerate(all_rows, start=first_row_number)
    )
    yield from _iter_row_bundles(context, row_entries, chunk_char_limit)


def _chunks_for_sheet(sheet_name: str, rows: list[list[str]], chunk_char_limit: int) -> list[str]:
    header = _first_header(rows)
    if header is None:
        return []
    header_index, headers = header
    wide_layout = _wide_metric_layout(headers)
    if wide_layout is not None:
        return _wide_metric_chunks(
            sheet_name,
            rows[header_index + 1 :],
            header_index + 2,
            headers,
            wide_layout,
            chunk_char_limit,
        )
    return list(
        _iter_bundled_standard_chunks(
            sheet_name,
            iter(rows[header_index + 1 :]),
            header_index + 2,
            headers,
            chunk_char_limit,
        )
    )


def _crosstable_chunks_for_sheet(
    sheet_name: str,
    rows: list[SheetRow],
    starts: list[CrosstableStart],
    chunk_char_limit: int,
) -> list[str]:
    if not rows:
        return []
    value_kind = "frequency" if sheet_name == "FREQ" else "percent"
    max_row_number = max(row.row_number for row in rows)
    chunks: list[str] = []
    for index, start in enumerate(starts):
        end_row = starts[index + 1].start_row - 1 if index + 1 < len(starts) else max_row_number
        subtable_rows = [row for row in rows if start.start_row <= row.row_number <= end_row]
        chunks.extend(
            _chunks_for_crosstable_subtable(
                sheet_name,
                value_kind,
                start,
                end_row,
                subtable_rows,
                chunk_char_limit,
            )
        )
    return chunks


def _chunks_for_crosstable_subtable(
    sheet_name: str,
    value_kind: str,
    start: CrosstableStart,
    end_row: int,
    rows: list[SheetRow],
    chunk_char_limit: int,
) -> list[str]:
    if len(rows) < 2:
        return []
    data_index = _first_crosstable_data_index(rows)
    header_rows = [row.values for row in rows[1:data_index]]
    data_rows = rows[data_index:]
    if not data_rows:
        return []
    column_count = max(len(row.values) for row in rows)
    headers = _flatten_multirow_headers(header_rows[:5], column_count)
    range_label = f"A{start.start_row}:{_column_letter(column_count)}{end_row}"
    metadata = (
        f"시트: {sheet_name} | 표유형: {value_kind} | 표번호: {start.table_number} | "
        f"표제목: {start.title} | BASE: {start.base} | N: {start.sample_size} | 범위: {range_label}"
    )
    metadata, headers = _hoist_common_header_prefix(metadata, headers)
    context = metadata + "\n표머리: " + " | ".join(headers)
    use_bundle = len(context) <= chunk_char_limit - BUNDLE_MIN_ROW_RESERVE
    if use_bundle:
        sample = [
            (
                _bundle_row_line(row.row_number, row.values),
                _crosstable_row_line(metadata, row, headers),
            )
            for row in data_rows[:BUNDLE_SAMPLE_ROWS]
        ]
        use_bundle = _bundle_saves_chars(sample, len(context), chunk_char_limit)
    if not use_bundle:
        # 문맥이 너무 길거나 묶음 이득이 없으면 기존 행별 메타데이터+헤더-값 쌍 형식을 유지한다.
        return list(
            _iter_packed_lines(
                (
                    line
                    for row in data_rows
                    for line in _crosstable_fallback_lines(metadata, row, headers, chunk_char_limit)
                ),
                chunk_char_limit,
            )
        )
    row_entries = (
        (
            _bundle_row_line(row.row_number, row.values),
            lambda item=row: _crosstable_fallback_lines(metadata, item, headers, chunk_char_limit),
        )
        for row in data_rows
    )
    return list(_iter_row_bundles(context, row_entries, chunk_char_limit))


def _crosstable_fallback_lines(
    metadata: str,
    row: SheetRow,
    headers: list[str],
    chunk_char_limit: int,
) -> list[str]:
    line = _crosstable_row_line(metadata, row, headers)
    if not line:
        return []
    if len(line) > chunk_char_limit:
        return _split_long_line(line, chunk_char_limit)
    return [line]


def _weekly_canvas_chunks_for_sheet(
    sheet_name: str,
    rows: list[SheetRow],
    chunk_char_limit: int,
) -> list[str]:
    if not _is_weekly_canvas_sheet(sheet_name, rows):
        return []
    chunks: list[str] = []
    for panel in WEEKLY_CANVAS_PANELS:
        chunks.extend(_weekly_canvas_panel_chunks(sheet_name, rows, panel, chunk_char_limit))
    return chunks


def _is_weekly_canvas_sheet(sheet_name: str, rows: list[SheetRow]) -> bool:
    if sheet_name != "통계":
        return False
    top_text = " ".join(
        _clean_value(value)
        for row in rows[:20]
        for value in row.values
        if _clean_value(value)
    )
    return all(token in top_text for token in WEEKLY_CANVAS_TITLE_TOKENS)


def _weekly_canvas_panel_chunks(
    sheet_name: str,
    rows: list[SheetRow],
    panel: WeeklyCanvasPanel,
    chunk_char_limit: int,
) -> list[str]:
    tables = _weekly_canvas_tables(rows, panel)
    if not tables:
        return []
    consumed_rows = _weekly_canvas_consumed_rows(tables)
    chunks = _weekly_canvas_narrative_chunks(sheet_name, rows, panel, consumed_rows, chunk_char_limit)
    for table in tables:
        chunks.extend(_weekly_canvas_table_chunks(sheet_name, rows, panel, table, chunk_char_limit))
    return chunks


def _weekly_canvas_tables(rows: list[SheetRow], panel: WeeklyCanvasPanel) -> list[WeeklyCanvasTable]:
    title_indexes = [
        index
        for index, row in enumerate(rows)
        if _is_weekly_canvas_table_title(_weekly_canvas_first_value(row, panel))
    ]
    tables: list[WeeklyCanvasTable] = []
    for order, title_index in enumerate(title_indexes):
        next_title_index = title_indexes[order + 1] if order + 1 < len(title_indexes) else len(rows)
        data_start_index = _weekly_canvas_data_start_index(rows, panel, title_index + 1, next_title_index)
        if data_start_index is None:
            continue
        data_indexes = [
            index
            for index in range(data_start_index, next_title_index)
            if _weekly_canvas_numeric_count(_weekly_canvas_panel_values(rows[index], panel)) >= 2
        ]
        if not data_indexes:
            continue
        title = _weekly_canvas_first_value(rows[title_index], panel)
        tables.append(
            WeeklyCanvasTable(
                title=title,
                start_row=rows[title_index].row_number,
                end_row=rows[data_indexes[-1]].row_number,
                data_start_index=data_start_index,
                row_indexes=data_indexes,
            )
        )
    return tables


def _weekly_canvas_consumed_rows(tables: list[WeeklyCanvasTable]) -> set[int]:
    consumed: set[int] = set()
    for table in tables:
        consumed.add(table.start_row)
        consumed.update(range(table.start_row + 1, table.end_row + 1))
    return consumed


def _weekly_canvas_narrative_chunks(
    sheet_name: str,
    rows: list[SheetRow],
    panel: WeeklyCanvasPanel,
    consumed_rows: set[int],
    chunk_char_limit: int,
) -> list[str]:
    chunks: list[str] = []
    current = ""
    for row in rows:
        if row.row_number in consumed_rows:
            continue
        values = [_clean_value(value) for value in _weekly_canvas_panel_values(row, panel)]
        values = [value for value in values if value]
        if not values:
            continue
        panel_title = _weekly_canvas_panel_title(rows, panel, row.row_number)
        metadata = f"시트: {sheet_name} | 문서유형: weekly_canvas | 패널: {panel.label} | 패널제목: {panel_title}"
        line = f"{metadata} | narrative 행: {row.row_number} | " + " | ".join(values)
        if len(line) > chunk_char_limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_split_long_line(line, chunk_char_limit))
            continue
        if current and len(current) + 1 + len(line) > chunk_char_limit:
            chunks.append(current)
            current = line
        else:
            current = line if not current else f"{current}\n{line}"
    if current:
        chunks.append(current)
    return chunks


def _weekly_canvas_table_chunks(
    sheet_name: str,
    rows: list[SheetRow],
    panel: WeeklyCanvasPanel,
    table: WeeklyCanvasTable,
    chunk_char_limit: int,
) -> list[str]:
    row_by_index = {index: row for index, row in enumerate(rows)}
    title_index = next(index for index, row in row_by_index.items() if row.row_number == table.start_row)
    header_rows = [
        _weekly_canvas_panel_values(row_by_index[index], panel)
        for index in range(title_index + 1, table.data_start_index)
    ]
    column_count = panel.end_column - panel.start_column + 1
    headers = _flatten_multirow_headers(header_rows[:5], column_count)
    panel_title = _weekly_canvas_panel_title(rows, panel, table.start_row)
    range_label = f"{_column_letter(panel.start_column)}{table.start_row}:{_column_letter(panel.end_column)}{table.end_row}"
    metadata = (
        f"시트: {sheet_name} | 문서유형: weekly_canvas | 패널: {panel.label} | "
        f"패널제목: {panel_title} | 표제목: {table.title} | 범위: {range_label}"
    )
    chunks: list[str] = []
    current = ""
    for index in table.row_indexes:
        row = row_by_index[index]
        line = _weekly_canvas_row_line(metadata, row.row_number, _weekly_canvas_panel_values(row, panel), headers)
        if not line:
            continue
        if len(line) > chunk_char_limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_split_long_line(line, chunk_char_limit))
            continue
        if current and len(current) + 1 + len(line) > chunk_char_limit:
            chunks.append(current)
            current = line
        else:
            current = line if not current else f"{current}\n{line}"
    if current:
        chunks.append(current)
    return chunks


def _weekly_canvas_panel_title(
    rows: list[SheetRow],
    panel: WeeklyCanvasPanel,
    before_row_number: int | None = None,
) -> str:
    title = ""
    for row in rows:
        if before_row_number is not None and row.row_number > before_row_number:
            break
        value = _value_at_column(row, panel.title_column)
        if "주간 Sell out Trend" in value:
            title = _clean_value(value)
    return title or panel.label


def _weekly_canvas_data_start_index(
    rows: list[SheetRow],
    panel: WeeklyCanvasPanel,
    start_index: int,
    end_index: int,
) -> int | None:
    for index in range(start_index, end_index):
        values = _weekly_canvas_panel_values(rows[index], panel)
        if _weekly_canvas_numeric_count(values) >= 2:
            return index
    return None


def _is_weekly_canvas_table_title(value: str) -> bool:
    cleaned = _clean_value(value)
    return bool(re.match(r"\d+\.\s+.+도매\s+Sell-out\s+Trend", cleaned))


def _weekly_canvas_row_line(metadata: str, row_number: int, row: list[str], headers: list[str]) -> str:
    pairs: list[str] = []
    for index, value in enumerate(row):
        cleaned = _clean_value(value)
        if not cleaned:
            continue
        header = headers[index] if index < len(headers) else f"컬럼{index + 1}"
        pairs.append(f"{header}: {cleaned}")
    if not pairs:
        return ""
    return f"{metadata} | 행: {row_number} | " + " | ".join(pairs)


def _weekly_canvas_first_value(row: SheetRow, panel: WeeklyCanvasPanel) -> str:
    for value in _weekly_canvas_panel_values(row, panel):
        cleaned = _clean_value(value)
        if cleaned:
            return cleaned
    return ""


def _weekly_canvas_panel_values(row: SheetRow, panel: WeeklyCanvasPanel) -> list[str]:
    return [_value_at_column(row, column) for column in range(panel.start_column, panel.end_column + 1)]


def _value_at_column(row: SheetRow, column_number: int) -> str:
    index = column_number - 1
    return row.values[index] if index < len(row.values) else ""


def _weekly_canvas_numeric_count(values: list[str]) -> int:
    return sum(_looks_numeric(_clean_value(value)) for value in values)


def _first_crosstable_data_index(rows: list[SheetRow]) -> int:
    for index, row in enumerate(rows[1:], start=1):
        values = [_clean_value(value) for value in row.values]
        numeric_count = sum(_looks_numeric(value) for value in values[1:])
        if values and values[0] and numeric_count >= 2:
            return index
    return min(1 + min(5, max(len(rows) - 1, 0)), len(rows))


def _flatten_multirow_headers(header_rows: list[list[str]], column_count: int) -> list[str]:
    headers: list[str] = []
    for column_index in range(column_count):
        parts: list[str] = []
        for row in header_rows:
            value = _clean_header(row[column_index]) if column_index < len(row) else ""
            if not value or _is_formula_helper_header(value):
                continue
            if value not in parts:
                parts.append(value)
        headers.append(" / ".join(parts))
    return _dedupe_headers(headers)


def _is_formula_helper_header(value: str) -> bool:
    compact = value.replace(" ", "")
    return compact.startswith("=") or bool(re.fullmatch(r"[A-Z]+\d+(?:&[A-Z]+\d+)+", compact))


def _looks_numeric(value: str) -> bool:
    cleaned = value.strip().replace(",", "").replace("$", "").replace("%", "")
    if not cleaned:
        return False
    try:
        Decimal(cleaned)
    except InvalidOperation:
        return False
    return True


def _crosstable_row_line(metadata: str, row: SheetRow, headers: list[str]) -> str:
    pairs: list[str] = []
    for index, value in enumerate(row.values):
        cleaned = _clean_value(value)
        if not cleaned:
            continue
        header = headers[index] if index < len(headers) else f"컬럼{index + 1}"
        pairs.append(f"{header}: {cleaned}")
    if not pairs:
        return ""
    return f"{metadata} | 행: {row.row_number} | " + " | ".join(pairs)


def _column_letter(column_number: int) -> str:
    letters: list[str] = []
    current = max(column_number, 1)
    while current:
        current, remainder = divmod(current - 1, 26)
        letters.append(chr(65 + remainder))
    return "".join(reversed(letters))


def _first_header(rows: list[list[str]]) -> tuple[int, list[str]] | None:
    candidates: list[tuple[int, int, list[str]]] = []
    for index, row in enumerate(rows[:10]):
        cleaned = [_clean_header(value) for value in row]
        score = sum(bool(value) for value in cleaned)
        if score >= 2:
            candidates.append((score, index, cleaned))
    if candidates:
        _score, index, cleaned = max(candidates, key=lambda item: (item[0], -item[1]))
        headers = _merge_group_headers(rows[:index], cleaned)
        return index, _dedupe_headers(headers)
    return None


def _merge_group_headers(previous_rows: list[list[str]], header_row: list[str]) -> list[str]:
    headers: list[str] = []
    for column_index, header in enumerate(header_row):
        groups: list[str] = []
        for previous in previous_rows:
            value = _clean_header(previous[column_index]) if column_index < len(previous) else ""
            if value and value not in groups:
                groups.append(value)
        if header:
            groups.append(header)
        headers.append(" ".join(groups))
    return headers


def _dedupe_headers(headers: list[str]) -> list[str]:
    result: list[str] = []
    seen: dict[str, int] = {}
    for index, header in enumerate(headers, start=1):
        label = header or f"컬럼{index}"
        count = seen.get(label, 0) + 1
        seen[label] = count
        result.append(label if count == 1 else f"{label}_{count}")
    return result


def _row_line(sheet_name: str, row_number: int, headers: list[str], row: list[str]) -> str:
    pairs: list[str] = []
    for index, header in enumerate(headers):
        value = _clean_value(row[index]) if index < len(row) else ""
        if value:
            pairs.append(f"{header}: {value}")
    if not pairs:
        return ""
    return f"시트: {sheet_name} | 행: {row_number} | " + " | ".join(pairs)


def _wide_metric_layout(headers: list[str]) -> WideMetricLayout | None:
    fixed_columns: list[int] = []
    group_order: list[str] = []
    grouped_columns: dict[str, list[tuple[int, str]]] = {}
    for index, header in enumerate(headers):
        split_header = _split_metric_header(header)
        if split_header is None:
            fixed_columns.append(index)
            continue
        metric_name, period = split_header
        if metric_name not in grouped_columns:
            grouped_columns[metric_name] = []
            group_order.append(metric_name)
        grouped_columns[metric_name].append((index, period))
    if not group_order:
        return None
    metric_groups = [
        (metric_name, grouped_columns[metric_name])
        for metric_name in group_order
    ]
    return fixed_columns, metric_groups


def _split_metric_header(header: str) -> tuple[str, str] | None:
    parts = [part.strip() for part in header.splitlines() if part.strip()]
    if len(parts) < 2:
        return None
    return parts[0], " ".join(parts[1:])


def _iter_wide_metric_lines(
    sheet_name: str,
    data_rows: Iterable[list[str]],
    first_row_number: int,
    headers: list[str],
    layout: WideMetricLayout,
    chunk_char_limit: int,
) -> Iterator[str]:
    fixed_columns, metric_groups = layout
    sheet_name, headers = _hoist_common_header_prefix(sheet_name, headers)
    for row_number, row in enumerate(data_rows, start=first_row_number):
        fixed_pairs = [
            f"{headers[index]}: {_clean_value(row[index])}"
            for index in fixed_columns
            if index < len(row) and _clean_value(row[index])
        ]
        prefix = f"시트: {sheet_name} | 행: {row_number}"
        if fixed_pairs:
            prefix = f"{prefix} | " + " | ".join(fixed_pairs)
        for group_name, columns in metric_groups:
            metric_values = [
                (period, _clean_value(row[index]) if index < len(row) else "")
                for index, period in columns
            ]
            if not any(value for _period, value in metric_values):
                continue
            metric_pairs = [f"{period}={value}" for period, value in metric_values]
            line = f"{prefix} || {group_name}: " + ", ".join(metric_pairs)
            if len(line) > chunk_char_limit:
                yield from _split_wide_metric_line(prefix, group_name, metric_pairs, chunk_char_limit)
            else:
                yield line


def _wide_metric_chunks(
    sheet_name: str,
    data_rows: list[list[str]],
    first_row_number: int,
    headers: list[str],
    layout: WideMetricLayout,
    chunk_char_limit: int,
) -> list[str]:
    return list(
        _iter_packed_lines(
            _iter_wide_metric_lines(
                sheet_name, data_rows, first_row_number, headers, layout, chunk_char_limit
            ),
            chunk_char_limit,
        )
    )


def _iter_wide_metric_chunks(
    sheet_name: str,
    data_rows: Iterator[list[str]],
    first_row_number: int,
    headers: list[str],
    layout: WideMetricLayout,
    chunk_char_limit: int,
) -> Iterator[str]:
    yield from _iter_packed_lines(
        _iter_wide_metric_lines(
            sheet_name, data_rows, first_row_number, headers, layout, chunk_char_limit
        ),
        chunk_char_limit,
    )


def _iter_standard_chunks(
    sheet_name: str,
    data_rows: Iterator[list[str]],
    first_row_number: int,
    headers: list[str],
    chunk_char_limit: int,
) -> Iterator[str]:
    yield from _iter_bundled_standard_chunks(
        sheet_name,
        data_rows,
        first_row_number,
        headers,
        chunk_char_limit,
    )


def _split_wide_metric_line(
    prefix: str,
    group_name: str,
    metric_pairs: list[str],
    chunk_char_limit: int,
) -> list[str]:
    chunks: list[str] = []
    current_pairs: list[str] = []
    for pair in metric_pairs:
        candidate_pairs = [*current_pairs, pair]
        candidate = f"{prefix} || {group_name}: " + ", ".join(candidate_pairs)
        if len(candidate) <= chunk_char_limit:
            current_pairs = candidate_pairs
            continue
        if current_pairs:
            chunks.append(f"{prefix} || {group_name}: " + ", ".join(current_pairs))
            current_pairs = [pair]
        else:
            chunks.extend(_split_long_line(candidate, chunk_char_limit))
            current_pairs = []
    if current_pairs:
        chunks.append(f"{prefix} || {group_name}: " + ", ".join(current_pairs))
    return chunks


def _split_long_line(line: str, chunk_char_limit: int) -> list[str]:
    parts = line.split(" | ")
    chunks: list[str] = []
    current = ""
    for part in parts:
        candidate = part if not current else f"{current} | {part}"
        if len(candidate) <= chunk_char_limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = part[:chunk_char_limit]
    if current:
        chunks.append(current)
    return chunks


def _clean_header(value: str) -> str:
    cleaned = _clean_cell_text(value)
    if cleaned.lower() in {"none", "null", "nan", "unnamed"}:
        return ""
    return cleaned


def _clean_cell_text(value: str) -> str:
    lines = [" ".join(line.split()) for line in value.replace("\x00", " ").splitlines()]
    cleaned = [line for line in lines if line]
    return "\n".join(cleaned)


def _clean_value(value: str) -> str:
    return " ".join(value.replace("\x00", " ").split())
