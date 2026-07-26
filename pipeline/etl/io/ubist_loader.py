#!/usr/bin/env python3
"""Load UBIST xlsx raw sales into hive-partitioned Parquet.

The source workbook layout is fixed across the 53 UBIST workbooks:
row 1 contains metric names, row 2 contains dimensions or periods, and
data begins at row 3. The loader streams rows into one Parquet file per
period partition so the full load does not need to materialize in memory.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import sqlite3
import sys
import tempfile
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import openpyxl
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from pipeline.etl.lib.ops_utils import configure_logging, find_project_root
from pipeline.etl.lib.storage import get_data_path
from pipeline.etl.io.source_headers import normalize_source_header
from pipeline.etl.io.workbook_content import load_workbook_by_content


LOGGER = configure_logging(__name__)
ROOT = find_project_root(Path(__file__).resolve())
UBIST_ROOT = get_data_path(
    bucket_env="MINIO_BUCKET_RAW_UBIST",
    bucket_default="jw-market-raw-ubist",
    local_default=ROOT / "data" / "UBIST",
)
TARGET_DIR = ROOT / "output" / "ubist"
KST = ZoneInfo("Asia/Seoul")

CANONICAL_DIMENSIONS = [
    "제조사",
    "국내/외자",
    "판매사",
    "판매사2",
    "제품",
    "ATC",
    "브랜드",
    "약가",
    "성분",
    "성분용량",
    "일반/전문",
    "약품코드",
    "제형",
    "투여경로",
    "급여구분",
    "종별",
    "진료과",
    "연령",
    "성별",
]

PATENT_DIMENSIONS = [
    "물질특허만료일",
    "마지막특허만료일",
    "마지막특허특성",
    "PMS만료일",
    "약품허가일",
    "Generic",
]

METRIC_MAP = {
    "처방조제액(원)": "rx_amt",
    "처방건수_P": "rx_cnt",
    "처방량_P": "rx_qty",
}

PATENT_ALIASES = {
    "물질특허 만료일": "물질특허만료일",
    "마지막특허 만료일": "마지막특허만료일",
    "마지막특허 특성": "마지막특허특성",
}

GENERIC_VALUES = {
    "Original",
    "First Generic",
    "Generic",
    "개량신약(Super Generic)",
}

METRIC_COLUMNS = list(METRIC_MAP.values())
LINEAGE_COLUMNS = ["source_file", "source_folder", "source_sheet", "source_row_no", "ingested_at"]
STATIC_METADATA_COLUMNS = PATENT_DIMENSIONS
BUSINESS_GRAIN_COLUMNS = CANONICAL_DIMENSIONS + ["period_yyyymm"]
UBIST_NATURAL_KEY_COLUMNS = BUSINESS_GRAIN_COLUMNS
BUSINESS_METRIC_COLUMNS = BUSINESS_GRAIN_COLUMNS + METRIC_COLUMNS
DEDUP_SORT_COLUMNS = ["source_file", "source_sheet", "source_row_no"]
DEDUP_METADATA_SORT_COLUMNS = ["_static_meta_score"] + DEDUP_SORT_COLUMNS
COLUMNS = (
    CANONICAL_DIMENSIONS
    + PATENT_DIMENSIONS
    + METRIC_COLUMNS
    + ["period_yyyymm"]
    + LINEAGE_COLUMNS
)

STRING_COLUMNS = (
    CANONICAL_DIMENSIONS
    + PATENT_DIMENSIONS
    + ["period_yyyymm", "source_file", "source_folder", "source_sheet", "ingested_at"]
)

SCHEMA = pa.schema(
    [(col, pa.string()) for col in CANONICAL_DIMENSIONS + PATENT_DIMENSIONS]
    + [(metric, pa.float64()) for metric in METRIC_MAP.values()]
    + [
        ("period_yyyymm", pa.string()),
        ("source_file", pa.string()),
        ("source_folder", pa.string()),
        ("source_sheet", pa.string()),
        ("source_row_no", pa.int64()),
        ("ingested_at", pa.string()),
    ]
)


@dataclass
class SheetMapping:
    sheet_name: str
    dim_cols: list[tuple[int, str, str]]
    duplicate_cols: list[tuple[int, str, str]]
    metric_cols: list[tuple[int, str, str, str]]


@dataclass
class PartitionStats:
    row_count: int = 0
    source_files: set[str] = field(default_factory=set)


@dataclass
class DedupReport:
    period: str
    rows_before: int
    rows_after: int
    duplicate_groups: int
    duplicate_rows_removed: int
    conflict_groups: int
    conflict_rows: int


@dataclass(frozen=True)
class SourceSummary:
    path: Path
    source_file: str
    source_folder: str
    periods: tuple[str, ...]


@dataclass
class IncrementalPlan:
    add: list[SourceSummary]
    skip: list[SourceSummary]
    conflicts: list[dict[str, str]]
    loaded_source_files: set[str]


def now_kst() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


def normalize_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return unicodedata.normalize("NFC", text)


def to_string_or_none(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    text = str(value).strip()
    return unicodedata.normalize("NFC", text) if text else None


def to_number_or_none(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        if isinstance(value, float) and math.isnan(value):
            return None
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_period(value: object) -> str:
    text = normalize_text(value)
    if not text:
        raise ValueError(f"unparseable period: {value!r}")
    match = re.match(r"^(\d{4})\s*년\s*(\d{1,2})\s*월$", text)
    if match:
        return f"{match.group(1)}-{int(match.group(2)):02d}"
    raise ValueError(f"unparseable period: {value!r}")


def canonical_header(value: object) -> str | None:
    lookup = normalize_source_header(value)
    if not lookup:
        return None
    aliases = {normalize_source_header(source): target for source, target in PATENT_ALIASES.items()}
    canonical = {
        normalize_source_header(header): header
        for header in CANONICAL_DIMENSIONS + PATENT_DIMENSIONS
    }
    header = aliases.get(lookup) or canonical.get(lookup)
    if header:
        return header
    return None


def lookup_text(value: object) -> str | None:
    text = normalize_text(value)
    if not text:
        return None
    return re.sub(r"\s+", "", text).casefold()


def generic_lookup_keys(base: dict[str, object]) -> list[str]:
    keys: list[str] = []
    code = lookup_text(base.get("약품코드"))
    product = lookup_text(base.get("제품"))
    brand = lookup_text(base.get("브랜드"))
    if code:
        keys.append(f"code:{code}")
    if product:
        keys.append(f"product:{product}")
    if brand:
        keys.append(f"brand:{brand}")
    return keys


def normalize_generic_value(value: object) -> str | None:
    text = normalize_text(value)
    if text in GENERIC_VALUES:
        return text
    return None


def resolve_path(raw_path: str) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        path = ROOT / path
    if path.exists():
        return path

    candidates = [
        Path(unicodedata.normalize("NFC", str(path))),
        Path(unicodedata.normalize("NFD", str(path))),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate

    normalized_target = unicodedata.normalize("NFC", str(path))
    for candidate in ROOT.rglob(path.name):
        if unicodedata.normalize("NFC", str(candidate)) == normalized_target:
            return candidate

    raise FileNotFoundError(f"Path not found: {raw_path}")


def discover_xlsx(args: argparse.Namespace) -> list[Path]:
    paths: list[Path] = []
    if args.all or (getattr(args, "incremental", False) and not args.folder and not args.file):
        if not UBIST_ROOT.exists():
            raise FileNotFoundError(f"Missing UBIST root: {UBIST_ROOT}")
        # 엑셀 임시 잠금파일(~$) 제외 — 원본 아님.
        paths.extend(sorted(p for p in UBIST_ROOT.rglob("*.xlsx") if not p.name.startswith("~$")))
    if args.folder:
        folder = resolve_path(args.folder)
        # 엑셀 임시 잠금파일(~$) 제외 — 원본 아님.
        paths.extend(sorted(p for p in folder.rglob("*.xlsx") if not p.name.startswith("~$")))
    if args.file:
        paths.append(resolve_path(args.file))
    unique = sorted({p.resolve() for p in paths})
    if not unique:
        raise RuntimeError("No xlsx files selected. Use --all, --folder, or --file.")
    return unique


def source_folder_for(path: Path) -> str:
    try:
        return str(path.parent.relative_to(UBIST_ROOT))
    except ValueError:
        return str(path.parent)


def normalized_source_file(path_or_name: Path | str) -> str:
    name = path_or_name.name if isinstance(path_or_name, Path) else str(path_or_name)
    return unicodedata.normalize("NFC", name)


def classify_sheet(sheet_name: str, header1: tuple[object, ...], header2: tuple[object, ...]) -> SheetMapping:
    dim_cols: list[tuple[int, str, str]] = []
    duplicate_cols: list[tuple[int, str, str]] = []
    metric_cols: list[tuple[int, str, str, str]] = []
    seen_dims: set[str] = set()
    metric_headers = {normalize_source_header(metric): metric for metric in METRIC_MAP}

    for idx in range(max(len(header1), len(header2))):
        h1 = metric_headers.get(normalize_source_header(header1[idx] if idx < len(header1) else None))
        h2 = normalize_text(header2[idx] if idx < len(header2) else None)

        if h1 in METRIC_MAP:
            try:
                period = parse_period(h2)
            except ValueError:
                continue
            metric_cols.append((idx, h1, METRIC_MAP[h1], period))
            continue

        canonical = canonical_header(h2)
        if canonical:
            if canonical in seen_dims:
                duplicate_cols.append((idx, h2 or "", canonical))
            else:
                seen_dims.add(canonical)
                dim_cols.append((idx, h2 or "", canonical))

    if not metric_cols:
        raise RuntimeError(f"No metric columns found in sheet: {sheet_name}")
    return SheetMapping(sheet_name, dim_cols, duplicate_cols, metric_cols)


def row_has_identifier(base: dict[str, object]) -> bool:
    return any(base.get(col) for col in ("약품코드", "제품", "성분", "브랜드"))


def normalized_natural_key(row: dict[str, object]) -> tuple[str, ...]:
    values: list[str] = []
    for column in UBIST_NATURAL_KEY_COLUMNS:
        value = _dedup_key_value(row.get(column))
        if value is None:
            source = row.get("source_file") or "<unknown>"
            sheet = row.get("source_sheet") or "<unknown>"
            row_no = row.get("source_row_no") or "<unknown>"
            raise RuntimeError(
                f"missing UBIST natural key column {column!r} in {source} sheet={sheet} row={row_no}"
            )
        values.append(str(value))
    return tuple(values)


def insert_source_natural_key(
    connection: sqlite3.Connection,
    *,
    key: tuple[str, ...],
    source_file: str,
    source_sheet: str,
    source_row_no: int,
) -> None:
    encoded_key = json.dumps(key, ensure_ascii=False, separators=(",", ":"))
    try:
        connection.execute(
            "INSERT INTO natural_keys (key, source_file, source_sheet, source_row_no) VALUES (?, ?, ?, ?)",
            (encoded_key, source_file, source_sheet, source_row_no),
        )
    except sqlite3.IntegrityError as exc:
        first = connection.execute(
            "SELECT source_sheet, source_row_no FROM natural_keys WHERE key = ?",
            (encoded_key,),
        ).fetchone()
        first_sheet, first_row = first if first else ("<unknown>", "<unknown>")
        raise RuntimeError(
            "duplicate UBIST natural key in "
            f"{source_file}: first sheet={first_sheet} row={first_row}; "
            f"duplicate sheet={source_sheet} row={source_row_no}"
        ) from exc


def validate_source_natural_key(connection: sqlite3.Connection, row: dict[str, object]) -> None:
    key = normalized_natural_key(row)
    insert_source_natural_key(
        connection,
        key=key,
        source_file=str(row["source_file"]),
        source_sheet=str(row["source_sheet"]),
        source_row_no=int(row["source_row_no"]),
    )


def build_generic_lookup(xlsx_paths: list[Path]) -> dict[str, str]:
    """Build a source-derived Generic lookup from workbooks that include it.

    The 2026.03/04 UBIST workbooks do not carry the patent block, while the
    ingredient workbooks do.  We therefore derive Generic by stable identifiers
    before the row streaming pass and backfill rows that only have the core
    dimensions.
    """

    counts: dict[str, Counter[str]] = defaultdict(Counter)
    rows_seen = 0
    for xlsx_path in xlsx_paths:
        workbook = load_workbook_by_content(xlsx_path, read_only=True, data_only=True)
        try:
            for sheet_name in workbook.sheetnames:
                worksheet = workbook[sheet_name]
                rows_iter = worksheet.iter_rows(values_only=True)
                try:
                    header1 = next(rows_iter)
                    header2 = next(rows_iter)
                except StopIteration:
                    continue
                mapping = classify_sheet(sheet_name, header1, header2)
                if not any(canonical == "Generic" for _, _, canonical in mapping.dim_cols):
                    continue
                for row in rows_iter:
                    base = {canonical: to_string_or_none(row[idx] if idx < len(row) else None) for idx, _, canonical in mapping.dim_cols}
                    generic = normalize_generic_value(base.get("Generic"))
                    if not generic or not row_has_identifier(base):
                        continue
                    rows_seen += 1
                    for key in generic_lookup_keys(base):
                        counts[key][generic] += 1
        finally:
            workbook.close()

    lookup = {key: counter.most_common(1)[0][0] for key, counter in counts.items() if counter}
    LOGGER.info("generic lookup built rows=%s keys=%s", f"{rows_seen:,}", f"{len(lookup):,}")
    return lookup


def fill_generic_from_lookup(base: dict[str, object], generic_lookup: dict[str, str] | None) -> None:
    if normalize_generic_value(base.get("Generic")):
        return
    if not generic_lookup:
        return
    for key in generic_lookup_keys(base):
        generic = generic_lookup.get(key)
        if generic:
            base["Generic"] = generic
            return


def iter_xlsx_rows(
    xlsx_path: Path,
    generic_lookup: dict[str, str] | None = None,
    *,
    validate_natural_keys: bool = True,
):
    with tempfile.TemporaryDirectory(prefix="ubist-source-key-") as work_dir_name:
        key_db_path = Path(work_dir_name) / "natural_keys.sqlite"
        key_connection = sqlite3.connect(str(key_db_path))
        key_connection.execute(
            """
            CREATE TABLE natural_keys (
                key TEXT PRIMARY KEY,
                source_file TEXT NOT NULL,
                source_sheet TEXT NOT NULL,
                source_row_no INTEGER NOT NULL
            )
            """
        )
        workbook = load_workbook_by_content(xlsx_path, read_only=True, data_only=True)
        try:
            for sheet_name in workbook.sheetnames:
                worksheet = workbook[sheet_name]
                rows_iter = worksheet.iter_rows(values_only=True)
                try:
                    header1 = next(rows_iter)
                    header2 = next(rows_iter)
                except StopIteration:
                    continue

                mapping = classify_sheet(sheet_name, header1, header2)
                loaded_at = now_kst()

                for row_no, row in enumerate(rows_iter, start=3):
                    base = {canonical: to_string_or_none(row[idx] if idx < len(row) else None) for idx, _, canonical in mapping.dim_cols}
                    if not row_has_identifier(base):
                        continue
                    fill_generic_from_lookup(base, generic_lookup)
                    base.update(
                        {
                            "source_file": xlsx_path.name,
                            "source_folder": source_folder_for(xlsx_path),
                            "source_sheet": sheet_name,
                            "source_row_no": row_no,
                            "ingested_at": loaded_at,
                        }
                    )

                    period_metrics: dict[str, dict[str, float | None]] = defaultdict(dict)
                    for idx, _, metric, period in mapping.metric_cols:
                        period_metrics[period][metric] = to_number_or_none(row[idx] if idx < len(row) else None)

                    for period, metrics in period_metrics.items():
                        output = dict(base)
                        output.update(metrics)
                        output["period_yyyymm"] = period
                        if validate_natural_keys:
                            validate_source_natural_key(key_connection, output)
                        yield period, output
        finally:
            try:
                workbook.close()
            finally:
                key_connection.close()


def prepare_frame(rows: list[dict[str, object]]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    for col in COLUMNS:
        if col not in frame.columns:
            frame[col] = None
    for col in STRING_COLUMNS:
        frame[col] = frame[col].map(to_string_or_none)
    for metric in METRIC_MAP.values():
        frame[metric] = pd.to_numeric(frame[metric], errors="coerce")
    frame["source_row_no"] = pd.to_numeric(frame["source_row_no"], errors="coerce").fillna(0).astype("int64")
    return frame.reindex(columns=COLUMNS)


def _metric_tuple_key(row: pd.Series) -> tuple[float | None, ...]:
    values: list[float | None] = []
    for column in METRIC_COLUMNS:
        value = row[column]
        values.append(None if pd.isna(value) else float(value))
    return tuple(values)


def _has_metadata_value(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, float) and math.isnan(value):
        return False
    return bool(str(value).strip())


def _metadata_completeness_score(frame: pd.DataFrame) -> pd.Series:
    if not STATIC_METADATA_COLUMNS:
        return pd.Series(0, index=frame.index)
    return frame[STATIC_METADATA_COLUMNS].apply(lambda column: column.map(_has_metadata_value)).sum(axis=1)


def _dedup_key_value(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, str):
        text = unicodedata.normalize("NFC", value.strip())
        return text or None
    return value


def fact_metric_key_set(frame: pd.DataFrame) -> set[tuple[object, ...]]:
    prepared = frame.reindex(columns=BUSINESS_METRIC_COLUMNS)
    return {
        tuple(_dedup_key_value(value) for value in row)
        for row in prepared.itertuples(index=False, name=None)
    }


def fact_metric_source_map(frame: pd.DataFrame) -> dict[tuple[object, ...], set[str]]:
    prepared = frame.reindex(columns=BUSINESS_METRIC_COLUMNS + ["source_file"])
    sources: dict[tuple[object, ...], set[str]] = defaultdict(set)
    for row in prepared.itertuples(index=False, name=None):
        key = tuple(_dedup_key_value(value) for value in row[:-1])
        source = _dedup_key_value(row[-1])
        if source:
            sources[key].add(str(source))
    return sources


def fact_metric_overlaps(left: pd.DataFrame, right: pd.DataFrame) -> set[tuple[object, ...]]:
    return fact_metric_key_set(left) & fact_metric_key_set(right)


def deduplicate_business_grain(frame: pd.DataFrame, period: str) -> tuple[pd.DataFrame, DedupReport]:
    """Replace the 2026-02 should_skip workaround with metric-safe grain dedup.

    Lineage and static metadata columns are excluded from identity. Rows are
    collapsed only when the fact grain and metrics are identical, preferring the
    row with populated patent/PMS/approval metadata and then using the stable
    ``source_file, source_sheet, source_row_no`` order. If the same fact grain
    has different metrics, all rows are preserved and the conflict is reported
    so the loader never hides data loss behind deduplication.
    """
    if frame.empty:
        return frame, DedupReport(period, 0, 0, 0, 0, 0, 0)

    work = frame.reindex(columns=COLUMNS).copy()
    work["_metric_tuple"] = work.apply(_metric_tuple_key, axis=1)
    metric_counts = work.groupby(BUSINESS_GRAIN_COLUMNS, dropna=False)["_metric_tuple"].nunique()
    conflict_keys = metric_counts[metric_counts > 1].reset_index()[BUSINESS_GRAIN_COLUMNS]
    if conflict_keys.empty:
        work["_metric_conflict"] = False
    else:
        conflict_keys = conflict_keys.assign(_metric_conflict=True)
        work = work.merge(conflict_keys, on=BUSINESS_GRAIN_COLUMNS, how="left")
        work["_metric_conflict"] = work["_metric_conflict"].fillna(False).astype(bool)

    safe = work[~work["_metric_conflict"]]
    conflicts = work[work["_metric_conflict"]]
    duplicate_sizes = safe.groupby(BUSINESS_METRIC_COLUMNS, dropna=False).size()
    duplicate_groups = int((duplicate_sizes > 1).sum())
    duplicate_rows_removed = int((duplicate_sizes[duplicate_sizes > 1] - 1).sum())

    safe = safe.assign(_static_meta_score=_metadata_completeness_score(safe))
    stable_safe = safe.sort_values(
        DEDUP_METADATA_SORT_COLUMNS,
        ascending=[False, True, True, True],
        kind="mergesort",
    )
    deduped_safe = stable_safe.drop_duplicates(subset=BUSINESS_METRIC_COLUMNS, keep="first")
    result = pd.concat([deduped_safe, conflicts], ignore_index=True)
    result = result.drop(columns=["_metric_tuple", "_metric_conflict", "_static_meta_score"], errors="ignore")
    result = result.sort_values(DEDUP_SORT_COLUMNS, kind="mergesort").reset_index(drop=True)
    report = DedupReport(
        period=period,
        rows_before=len(frame),
        rows_after=len(result),
        duplicate_groups=duplicate_groups,
        duplicate_rows_removed=duplicate_rows_removed,
        conflict_groups=len(conflict_keys),
        conflict_rows=len(conflicts),
    )
    if report.conflict_groups:
        # Metric conflicts are never collapsed; warning keeps the data visible
        # while making the collision auditable in loader logs and reports.
        LOGGER.warning(
            "UBIST metric conflicts preserved period=%s groups=%s rows=%s",
            period,
            report.conflict_groups,
            report.conflict_rows,
        )
    if report.duplicate_rows_removed:
        LOGGER.info(
            "UBIST business-grain dedup period=%s duplicate_groups=%s removed=%s rows_before=%s rows_after=%s",
            period,
            report.duplicate_groups,
            report.duplicate_rows_removed,
            report.rows_before,
            report.rows_after,
        )
    return result.reindex(columns=COLUMNS), report


def deduplicate_partition_file(path: Path, period: str) -> DedupReport:
    temp_path = path.with_suffix(".dedup.tmp")
    quoted_columns = ", ".join(f'"{column}"' for column in COLUMNS)
    grain_columns = ", ".join(f'"{column}"' for column in BUSINESS_GRAIN_COLUMNS)
    metric_columns = ", ".join(f'"{column}"' for column in METRIC_COLUMNS)
    business_metric_columns = ", ".join(f'"{column}"' for column in BUSINESS_METRIC_COLUMNS)
    stable_order = ", ".join(f'"{column}" ASC NULLS LAST' for column in DEDUP_SORT_COLUMNS)
    metadata_score = " + ".join(
        f"CASE WHEN \"{column}\" IS NOT NULL AND trim(CAST(\"{column}\" AS VARCHAR)) <> '' THEN 1 ELSE 0 END"
        for column in STATIC_METADATA_COLUMNS
    ) or "0"

    with tempfile.TemporaryDirectory(prefix="ubist-dedup-", dir=path.parent) as work_dir_name:
        work_dir = Path(work_dir_name)
        spill_dir = work_dir / "spill"
        spill_dir.mkdir()
        connection = duckdb.connect(str(work_dir / "dedup.duckdb"))
        try:
            connection.execute("SET memory_limit='4GB'")
            connection.execute("SET threads=2")
            connection.execute("SET preserve_insertion_order=false")
            connection.execute("SET temp_directory=?", [str(spill_dir)])
            connection.execute(
                f"""
                CREATE TABLE annotated AS
                SELECT
                  {quoted_columns},
                  row_number() OVER () AS _source_ordinal,
                  ({metadata_score}) AS _static_meta_score,
                  count(DISTINCT row({metric_columns})) OVER (
                    PARTITION BY {grain_columns}
                  ) AS _metric_variants
                FROM read_parquet(?)
                """,
                [str(path)],
            )
            connection.execute(
                f"""
                CREATE TABLE safe_ranked AS
                SELECT
                  *,
                  count(*) OVER (
                    PARTITION BY {business_metric_columns}
                  ) AS _duplicate_size,
                  row_number() OVER (
                    PARTITION BY {business_metric_columns}
                    ORDER BY _static_meta_score DESC, {stable_order}, _source_ordinal ASC
                  ) AS _duplicate_rank
                FROM annotated
                WHERE _metric_variants = 1
                """
            )
            (
                rows_before,
                conflict_groups,
                conflict_rows,
                duplicate_groups,
                duplicate_rows_removed,
                rows_after,
            ) = connection.execute(
                f"""
                SELECT
                  (SELECT count(*) FROM annotated),
                  (SELECT count(DISTINCT row({grain_columns})) FROM annotated WHERE _metric_variants > 1),
                  (SELECT count(*) FROM annotated WHERE _metric_variants > 1),
                  (SELECT count(*) FROM safe_ranked WHERE _duplicate_rank = 1 AND _duplicate_size > 1),
                  (SELECT coalesce(sum(_duplicate_size - 1), 0) FROM safe_ranked WHERE _duplicate_rank = 1),
                  (SELECT count(*) FROM annotated WHERE _metric_variants > 1)
                    + (SELECT count(*) FROM safe_ranked WHERE _duplicate_rank = 1)
                """
            ).fetchone()
            temp_sql = str(temp_path).replace("'", "''")
            connection.execute(
                f"""
                COPY (
                  SELECT {quoted_columns}
                  FROM (
                    SELECT {quoted_columns}, _source_ordinal
                    FROM annotated
                    WHERE _metric_variants > 1
                    UNION ALL
                    SELECT {quoted_columns}, _source_ordinal
                    FROM safe_ranked
                    WHERE _duplicate_rank = 1
                  ) AS retained
                  ORDER BY {stable_order}, _source_ordinal ASC
                ) TO '{temp_sql}' (FORMAT PARQUET, COMPRESSION SNAPPY)
                """
            )
        finally:
            connection.close()

    report = DedupReport(
        period=period,
        rows_before=int(rows_before),
        rows_after=int(rows_after),
        duplicate_groups=int(duplicate_groups),
        duplicate_rows_removed=int(duplicate_rows_removed),
        conflict_groups=int(conflict_groups),
        conflict_rows=int(conflict_rows),
    )
    temp_path.replace(path)
    if report.conflict_groups:
        LOGGER.warning(
            "UBIST metric conflicts preserved period=%s groups=%s rows=%s",
            period,
            report.conflict_groups,
            report.conflict_rows,
        )
    if report.duplicate_rows_removed:
        LOGGER.info(
            "UBIST business-grain dedup period=%s duplicate_groups=%s removed=%s rows_before=%s rows_after=%s",
            period,
            report.duplicate_groups,
            report.duplicate_rows_removed,
            report.rows_before,
            report.rows_after,
        )
    return report


class PartitionWriter:
    def __init__(self, target_root: Path):
        self.target_root = target_root
        self.writers: dict[str, pq.ParquetWriter] = {}
        self.stats: dict[str, PartitionStats] = defaultdict(PartitionStats)
        self.loaded_existing: set[str] = set()

    def _path_for(self, period: str) -> Path:
        year, month = period.split("-")
        return self.target_root / f"year={year}" / f"month={month}" / "data.parquet"

    def _open_writer(self, period: str) -> pq.ParquetWriter:
        if period in self.writers:
            return self.writers[period]
        path = self._path_for(period)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and period not in self.loaded_existing:
            existing = pq.read_table(path).select(COLUMNS)
            path.unlink()
            writer = pq.ParquetWriter(path, SCHEMA, compression="snappy")
            writer.write_table(existing)
            stats = self.stats[period]
            stats.row_count += existing.num_rows
            if "source_file" in existing.column_names:
                stats.source_files.update(str(value.as_py()) for value in existing["source_file"].unique() if value.as_py())
            self.loaded_existing.add(period)
        else:
            writer = pq.ParquetWriter(path, SCHEMA, compression="snappy")
        self.writers[period] = writer
        return writer

    def write_rows(self, period: str, rows: list[dict[str, object]]) -> None:
        if not rows:
            return
        frame = prepare_frame(rows)
        table = pa.Table.from_pandas(frame, schema=SCHEMA, preserve_index=False)
        self._open_writer(period).write_table(table)
        stats = self.stats[period]
        stats.row_count += len(rows)
        stats.source_files.update(str(row["source_file"]) for row in rows)

    def close(self) -> None:
        for writer in self.writers.values():
            writer.close()
        self.writers.clear()


def flush_buffers(writer: PartitionWriter, buffers: dict[str, list[dict[str, object]]], *, final: bool = False) -> None:
    threshold = 25_000
    for period in list(buffers):
        if final or len(buffers[period]) >= threshold:
            rows = buffers.pop(period)
            writer.write_rows(period, rows)


def deduplicate_written_partitions(target: Path, stats: dict[str, PartitionStats]) -> list[DedupReport]:
    reports: list[DedupReport] = []
    for period in sorted(stats):
        path = PartitionWriter(target)._path_for(period)
        if not path.exists():
            continue
        LOGGER.info("UBIST partition dedup start period=%s path=%s", period, path)
        report = deduplicate_partition_file(path, period)
        reports.append(report)
        table = pq.read_table(path, columns=["source_file"])
        stats[period].row_count = table.num_rows
        stats[period].source_files = {str(value.as_py()) for value in table["source_file"].unique() if value.as_py()}
    total_removed = sum(report.duplicate_rows_removed for report in reports)
    total_conflicts = sum(report.conflict_groups for report in reports)
    if total_removed or total_conflicts:
        LOGGER.info(
            "UBIST business-grain dedup complete removed=%s conflict_groups=%s",
            total_removed,
            total_conflicts,
        )
    return reports


def iter_included_xlsx_rows(
    xlsx_path: Path,
    generic_lookup: dict[str, str] | None,
    exclude_periods: frozenset[str],
    *,
    _row_source=iter_xlsx_rows,
):
    """Yield (period, row) from a UBIST workbook, skipping excluded periods.

    R-1 rehearsals pin selected months to canonical parquet sidecars. Those
    months must NOT be materialized by s1: if s1 wrote them, the downstream
    ``install_ubist_sidecars`` step would collide with its no-overwrite guard.
    Excluding them here leaves the partition for the sidecar step to create.
    A period never appears mid-file, so filtering per (period, row) is exact.
    """
    for period, row in _row_source(xlsx_path, generic_lookup):
        if period in exclude_periods:
            continue
        yield period, row


def load_to_parquet(
    xlsx_paths: list[Path],
    target: Path,
    *,
    mode: str,
    truncate: bool,
    previous_manifest: dict[str, object] | None = None,
    exclude_periods: frozenset[str] = frozenset(),
) -> dict[str, PartitionStats]:
    timestamp = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
    tmp_target = target.parent / f"{target.name}.__tmp_{timestamp}"
    backup_target = target.parent / f"{target.name}.__backup_{timestamp}"

    if tmp_target.exists():
        shutil.rmtree(tmp_target)
    tmp_target.mkdir(parents=True)

    if mode == "append" and target.exists():
        shutil.copytree(target, tmp_target, dirs_exist_ok=True)
    elif mode != "replace":
        raise ValueError(f"Unsupported mode: {mode}")

    if exclude_periods:
        LOGGER.info("UBIST load excluding pinned sidecar periods=%s", sorted(exclude_periods))

    buffers: dict[str, list[dict[str, object]]] = defaultdict(list)
    writer = PartitionWriter(tmp_target)
    total_rows = 0
    generic_lookup = build_generic_lookup(xlsx_paths)
    try:
        for idx, xlsx_path in enumerate(xlsx_paths, start=1):
            LOGGER.info("[%s/%s] reading %s", idx, len(xlsx_paths), xlsx_path)
            for period, row in iter_included_xlsx_rows(xlsx_path, generic_lookup, exclude_periods):
                buffers[period].append(row)
                total_rows += 1
                if total_rows % 250_000 == 0:
                    LOGGER.info("rows=%s active_partitions=%s", f"{total_rows:,}", len(buffers))
                    flush_buffers(writer, buffers)
            flush_buffers(writer, buffers)
        flush_buffers(writer, buffers, final=True)
    finally:
        writer.close()

    leaked = exclude_periods & set(writer.stats)
    if leaked:
        # Fail closed: an excluded period must never be materialized by s1.
        raise RuntimeError(f"excluded UBIST periods leaked into load: {sorted(leaked)}")

    LOGGER.info(
        "UBIST stream load complete rows=%s partitions=%s; starting partition dedup",
        f"{total_rows:,}",
        len(writer.stats),
    )
    deduplicate_written_partitions(tmp_target, writer.stats)
    write_manifest(tmp_target, writer.stats, xlsx_paths, previous_manifest=previous_manifest)

    if target.exists():
        if truncate or mode == "replace":
            target.rename(backup_target)
        else:
            raise RuntimeError(f"Target already exists: {target}")
    tmp_target.rename(target)
    if backup_target.exists():
        shutil.rmtree(backup_target)
    LOGGER.info("loaded rows=%s partitions=%s target=%s", f"{total_rows:,}", len(writer.stats), target)
    return writer.stats


def partition_path(period: str) -> str:
    year, month = period.split("-")
    return f"year={year}/month={month}/data.parquet"


def write_manifest(
    target: Path,
    stats: dict[str, PartitionStats],
    xlsx_paths: list[Path],
    *,
    previous_manifest: dict[str, object] | None = None,
) -> None:
    generated_at = now_kst()
    previous_partitions = {}
    if previous_manifest:
        for entry in previous_manifest.get("partitions", []):
            if isinstance(entry, dict) and entry.get("period_yyyymm"):
                previous_partitions[str(entry["period_yyyymm"])] = dict(entry)
    partition_entries = previous_partitions
    for period in sorted(stats):
        partition_entries[period] = {
            "period_yyyymm": period,
            "path": partition_path(period),
            "row_count": stats[period].row_count,
            "source_files": sorted(stats[period].source_files),
            "loaded_at": generated_at,
        }
    source_file_count = len(
        {
            normalized_source_file(source)
            for entry in partition_entries.values()
            for source in entry.get("source_files", [])
        }
    )
    if not previous_manifest:
        source_file_count = len(xlsx_paths)
    manifest = {
        "schema_version": "1.0",
        "generated_at": generated_at,
        "storage": "parquet_hive_partition",
        "compression": "snappy",
        "metric_map": METRIC_MAP,
        "canonical_dimensions": CANONICAL_DIMENSIONS,
        "patent_dimensions": PATENT_DIMENSIONS,
        "dedup_identity": {
            "fact_grain_columns": BUSINESS_GRAIN_COLUMNS,
            "metric_columns": METRIC_COLUMNS,
            "excluded_lineage_columns": LINEAGE_COLUMNS,
            "excluded_static_metadata_columns": STATIC_METADATA_COLUMNS,
            "static_metadata_policy": "prefer populated metadata, then source_file/source_sheet/source_row_no",
            "metric_conflict_policy": "preserve all rows and report conflicts",
        },
        "dimension_duplicate_policy": {
            "decision": "collapse_duplicate_semantic_headers",
            "kept": "first occurrence in the fixed dimension block",
            "dropped_after_sample_match": ["제조사", "국내/외자", "판매사", "ATC", "성분"],
            "retained_distinct": ["판매사2"],
        },
        "source_file_count": source_file_count,
        "partitions": [partition_entries[period] for period in sorted(partition_entries)],
    }
    target.mkdir(parents=True, exist_ok=True)
    (target / "_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def dry_run(xlsx_paths: list[Path], limit_rows: int = 3) -> None:
    print("# UBIST Parquet Loader Dry Run\n")
    for xlsx_path in xlsx_paths:
        print(f"## Source: {xlsx_path}\n")
        workbook = load_workbook_by_content(xlsx_path, read_only=True, data_only=True)
        for sheet_name in workbook.sheetnames:
            worksheet = workbook[sheet_name]
            rows_iter = worksheet.iter_rows(values_only=True)
            header1 = next(rows_iter)
            header2 = next(rows_iter)
            mapping = classify_sheet(sheet_name, header1, header2)
            periods = sorted({period for _, _, _, period in mapping.metric_cols})
            metrics = sorted({metric for _, _, metric, _ in mapping.metric_cols})
            print(f"### Sheet: {sheet_name}")
            print(f"- rows: {worksheet.max_row:,}")
            print(f"- cols: {worksheet.max_column:,}")
            print(f"- kept dimensions: {len(mapping.dim_cols)}")
            for idx, raw, canonical in mapping.dim_cols:
                print(f"  - col {idx + 1}: {raw} -> {canonical}")
            print(f"- duplicate dimensions dropped: {len(mapping.duplicate_cols)}")
            for idx, raw, canonical in mapping.duplicate_cols:
                print(f"  - col {idx + 1}: {raw} -> {canonical}")
            print(f"- metrics: {metrics}")
            print(f"- periods: {periods[0]} .. {periods[-1]} ({len(periods)})")
            print("\n#### Sample Output Rows")
            samples = []
            for _, row in iter_xlsx_rows(xlsx_path):
                samples.append(row)
                if len(samples) >= limit_rows:
                    break
            frame = prepare_frame(samples)
            print(frame.head(limit_rows).to_string(index=False))
            print()


def read_manifest(target: Path) -> dict[str, object]:
    manifest_path = target / "_manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Missing UBIST manifest: {manifest_path}")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def manifest_source_files(manifest: dict[str, object]) -> set[str]:
    return {
        normalized_source_file(source)
        for entry in manifest.get("partitions", [])
        if isinstance(entry, dict)
        for source in entry.get("source_files", [])
    }


def summarize_source(path: Path) -> SourceSummary:
    periods: set[str] = set()
    workbook = load_workbook_by_content(path, read_only=True, data_only=True)
    try:
        for sheet_name in workbook.sheetnames:
            worksheet = workbook[sheet_name]
            rows_iter = worksheet.iter_rows(values_only=True)
            try:
                header1 = next(rows_iter)
                header2 = next(rows_iter)
            except StopIteration:
                continue
            mapping = classify_sheet(sheet_name, header1, header2)
            periods.update(period for _, _, _, period in mapping.metric_cols)
    finally:
        workbook.close()
    return SourceSummary(
        path=path,
        source_file=normalized_source_file(path),
        source_folder=unicodedata.normalize("NFC", source_folder_for(path)),
        periods=tuple(sorted(periods)),
    )


def existing_partition_for(target: Path, period: str) -> Path:
    year, month = period.split("-")
    return target / f"year={year}" / f"month={month}" / "data.parquet"


def source_period_frame(path: Path, period: str) -> pd.DataFrame:
    rows = [
        row
        for row_period, row in iter_xlsx_rows(path, validate_natural_keys=False)
        if row_period == period
    ]
    return prepare_frame(rows)


def source_period_frames(path: Path, periods: set[str]) -> dict[str, pd.DataFrame]:
    rows_by_period: dict[str, list[dict[str, object]]] = {period: [] for period in periods}
    for row_period, row in iter_xlsx_rows(path, validate_natural_keys=False):
        if row_period in rows_by_period:
            rows_by_period[row_period].append(row)
    return {period: prepare_frame(rows) for period, rows in rows_by_period.items()}


def incremental_plan(xlsx_paths: list[Path], target: Path) -> IncrementalPlan:
    manifest = read_manifest(target)
    loaded = manifest_source_files(manifest)
    summaries = [summarize_source(path) for path in xlsx_paths]
    add = [summary for summary in summaries if summary.source_file not in loaded]
    skip = [summary for summary in summaries if summary.source_file in loaded]

    known_paths = {normalized_source_file(path): path for path in xlsx_paths}
    if UBIST_ROOT.exists():
        for path in UBIST_ROOT.rglob("*.xlsx"):
            if not path.name.startswith("~$"):
                known_paths.setdefault(normalized_source_file(path), path.resolve())
    by_name = {summary.source_file: summary for summary in summaries}
    existing: list[SourceSummary] = []
    for name in sorted(loaded):
        if name in by_name:
            existing.append(by_name[name])
        elif name in known_paths:
            existing.append(summarize_source(known_paths[name]))
    conflicts = detect_period_conflicts(add, existing)
    conflicts.extend(detect_content_overlaps(add, target))
    return IncrementalPlan(add=add, skip=skip, conflicts=conflicts, loaded_source_files=loaded)


def detect_period_conflicts(add: list[SourceSummary], existing: list[SourceSummary]) -> list[dict[str, str]]:
    conflicts: list[dict[str, str]] = []
    seen: dict[tuple[str, str], SourceSummary] = {}

    # 같은 원천 폴더에서 같은 월을 두 파일이 제공하면 실제 metric-key 충돌
    # 가능성이 높다. 예: 종병 2501-07과 종병 2507-12의 2025-07 겹침.
    for summary in existing + add:
        for period in summary.periods:
            key = (summary.source_folder, period)
            previous = seen.get(key)
            if previous and (summary in add or previous in add):
                conflicts.append(
                    {
                        "period_yyyymm": period,
                        "source_folder": summary.source_folder,
                        "left": previous.source_file,
                        "right": summary.source_file,
                        "reason": "same source_folder period overlap",
                    }
                )
            else:
                seen[key] = summary
    return conflicts


def detect_content_overlaps(add: list[SourceSummary], target: Path) -> list[dict[str, str]]:
    conflicts: list[dict[str, str]] = []
    existing_cache: dict[str, tuple[pd.DataFrame, dict[tuple[object, ...], set[str]]]] = {}

    for summary in add:
        existing_periods = {period for period in summary.periods if existing_partition_for(target, period).exists()}
        if not existing_periods:
            continue
        frames_by_period = source_period_frames(summary.path, existing_periods)
        for period in sorted(existing_periods):
            partition = existing_partition_for(target, period)
            if period not in existing_cache:
                existing_frame = pq.read_table(partition, columns=BUSINESS_METRIC_COLUMNS + ["source_file"]).to_pandas()
                existing_cache[period] = (existing_frame, fact_metric_source_map(existing_frame))
            existing_frame, source_map = existing_cache[period]
            candidate_frame = frames_by_period[period]
            overlaps = fact_metric_overlaps(existing_frame, candidate_frame)
            if not overlaps:
                continue
            left_sources = sorted({source for key in overlaps for source in source_map.get(key, set())})
            conflicts.append(
                {
                    "period_yyyymm": period,
                    "source_folder": summary.source_folder,
                    "left": ", ".join(left_sources[:5]) if left_sources else "existing partition",
                    "right": summary.source_file,
                    "reason": "content-level fact+metric overlap",
                    "overlap_keys": str(len(overlaps)),
                }
            )
    return conflicts


def print_incremental_plan(plan: IncrementalPlan) -> None:
    print("# UBIST Incremental Plan\n")
    print(f"- loaded source files in manifest: {len(plan.loaded_source_files)}")
    print(f"- add candidates: {len(plan.add)}")
    print(f"- skipped already loaded: {len(plan.skip)}")
    print(f"- conflicts: {len(plan.conflicts)}\n")

    print("## ADD")
    for summary in plan.add:
        print(f"- {summary.path} | periods={summary.periods[0]}..{summary.periods[-1]} ({len(summary.periods)})")
    print("\n## SKIP")
    for summary in plan.skip:
        print(f"- {summary.path}")
    if plan.conflicts:
        print("\n## CONFLICTS")
        for conflict in plan.conflicts:
            print(
                "- {period_yyyymm} | {source_folder} | {left} <> {right} | {reason}".format(
                    **conflict
                )
            )


def run_incremental_ubist_load(
    *,
    target: Path,
    paths: list[Path] | None = None,
    folder: Path | None = None,
    file: Path | None = None,
    all_sources: bool = True,
    dry: bool = False,
    allow_overlap_dedup: bool = False,
) -> dict[str, PartitionStats]:
    args = argparse.Namespace(
        all=all_sources,
        folder=str(folder) if folder is not None else None,
        file=str(file) if file is not None else None,
        dry_run=dry,
        truncate=False,
        mode="append",
        target_dir=str(target),
        incremental=True,
    )
    xlsx_paths = [p.resolve() for p in paths] if paths is not None else discover_xlsx(args)
    manifest = read_manifest(target)
    plan = incremental_plan(xlsx_paths, target)
    print_incremental_plan(plan)
    if dry:
        return {}
    if plan.conflicts and not allow_overlap_dedup:
        raise RuntimeError("UBIST incremental load stopped: period conflicts found")
    if plan.conflicts and allow_overlap_dedup:
        LOGGER.warning(
            "UBIST incremental period conflicts allowed for append+dedup conflicts=%s",
            len(plan.conflicts),
        )
    if not plan.add:
        LOGGER.info("UBIST incremental load has no new source files target=%s", target)
        return {}
    return load_to_parquet(
        [summary.path for summary in plan.add],
        target,
        mode="append",
        truncate=True,
        previous_manifest=manifest,
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=False)
    source.add_argument("--folder", help="Folder containing UBIST xlsx files")
    source.add_argument("--file", help="Single UBIST xlsx file")
    source.add_argument("--all", action="store_true", help="Load all xlsx files below data/UBIST")
    parser.add_argument("--incremental", action="store_true", help="Compare source files to target _manifest.json and append only new files")
    parser.add_argument(
        "--allow-overlap-dedup",
        action="store_true",
        help="Allow same-folder period overlap during incremental append; partition dedup keeps identical business+metric rows.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Analyze schema and sample rows without writing")
    parser.add_argument("--truncate", action="store_true", help="Replace the existing output/ubist target")
    parser.add_argument("--mode", choices=["replace", "append"], default="replace")
    parser.add_argument("--target-dir", default=str(TARGET_DIR))
    args = parser.parse_args(argv)
    if not args.incremental and not (args.folder or args.file or args.all):
        parser.error("one of --folder, --file, or --all is required unless --incremental is used")
    return args


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        xlsx_paths = discover_xlsx(args)
        if args.incremental:
            stats = run_incremental_ubist_load(
                target=Path(args.target_dir),
                paths=xlsx_paths,
                dry=args.dry_run,
                allow_overlap_dedup=args.allow_overlap_dedup,
            )
            if not args.dry_run:
                LOGGER.info("partition summary")
                for period in sorted(stats):
                    LOGGER.info("%s: %s", period, f"{stats[period].row_count:,}")
            return 0
        if args.dry_run:
            dry_run(xlsx_paths)
            return 0
        stats = load_to_parquet(xlsx_paths, Path(args.target_dir), mode=args.mode, truncate=args.truncate)
        LOGGER.info("partition summary")
        for period in sorted(stats):
            LOGGER.info("%s: %s", period, f"{stats[period].row_count:,}")
    except Exception as exc:
        LOGGER.error("ERROR: %s", exc)
        return 1
    return 0


def run_ubist_load(
    *,
    target: Path,
    mode: str = "replace",
    truncate: bool = True,
    paths: list[Path] | None = None,
    folder: Path | None = None,
    file: Path | None = None,
    all_sources: bool = True,
    exclude_periods: frozenset[str] = frozenset(),
) -> dict[str, PartitionStats]:
    args = argparse.Namespace(
        all=all_sources,
        folder=str(folder) if folder is not None else None,
        file=str(file) if file is not None else None,
        dry_run=False,
        truncate=truncate,
        mode=mode,
        target_dir=str(target),
    )
    xlsx_paths = [p.resolve() for p in paths] if paths is not None else discover_xlsx(args)
    return load_to_parquet(
        xlsx_paths, target, mode=mode, truncate=truncate, exclude_periods=exclude_periods
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
