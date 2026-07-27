#!/usr/bin/env python3
"""Load UBIST xlsx raw sales into hive-partitioned Parquet.

The source workbook layout is fixed across the 53 UBIST workbooks:
row 1 contains metric names, row 2 contains dimensions or periods, and
data begins at row 3. The loader streams rows into one Parquet file per
period partition so the full load does not need to materialize in memory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
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
BUSINESS_METRIC_COLUMNS = BUSINESS_GRAIN_COLUMNS + METRIC_COLUMNS
DEDUP_SORT_COLUMNS = ["source_file", "source_sheet", "source_row_no"]
DEDUP_METADATA_SORT_COLUMNS = ["_static_meta_score"] + DEDUP_SORT_COLUMNS

# 기간 단위 교체 --------------------------------------------------------------
# 같은 기간을 다시 올리면 그 기간에서 ★그 업로드가 담당하던 몫을 통째로 바꾼다.
#
# ★ INGEST_PERIOD_REPLACE=0 으로 두면 예전 append+dedup 경로로 되돌아간다.
#   그 경로에서 정정본은 다음과 같이 처리된다. 어느 쪽도 "정정 반영" 이 아니다.
#     · 같은 파일명 재업로드 → manifest 이름 대조에서 걸러져 ★아예 적재되지 않는다
#     · 측정값 정정        → 옛 행과 새 행이 ★공존하고 mart 가 SUM 하여 이중계상된다
#                            (io/mart/general_ubist.py 의 SUM(rx_amt))
#     · 특허/PMS 등 정정   → 이 6개 열은 grain 에도 dedup 키에도 없어 conflict 로
#                            잡히지 않으며, 생존 행은 ★source_file 알파벳 순서가 정한다
#   즉 0 은 "안전한 기본값" 이 아니라 ★정정본이 조용히 유실되는 설정이다.
INGEST_PERIOD_REPLACE_ENV = "INGEST_PERIOD_REPLACE"
INGEST_PERIOD_REPLACE_SCOPE_ENV = "INGEST_PERIOD_REPLACE_SCOPE"
REPLACE_SCOPE_SOURCE_FILE = "source_file"
REPLACE_SCOPE_SOURCE_FOLDER = "source_folder"
REPLACE_SCOPE_PERIOD = "period"
REPLACE_SCOPES = (REPLACE_SCOPE_SOURCE_FILE, REPLACE_SCOPE_SOURCE_FOLDER, REPLACE_SCOPE_PERIOD)
FALSEY = {"0", "false", "no", "off"}
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
    digest: str = ""


@dataclass(frozen=True)
class PurgeTarget:
    """교체를 위해 기존 파티션에서 걷어낼 행의 범위."""

    period: str
    scope: str
    value: str


@dataclass
class IncrementalPlan:
    add: list[SourceSummary]
    skip: list[SourceSummary]
    conflicts: list[dict[str, str]]
    loaded_source_files: set[str]
    # 이름은 이미 적재됐지만 내용 digest 가 달라진 정정본. 옛 몫을 걷어내고 다시 넣는다.
    replace: list[SourceSummary] = field(default_factory=list)
    # 구 manifest 라 digest 가 없어 정정 여부를 ★판정할 수 없는 파일.
    # 모른다는 사실을 성공으로 적지 않기 위해 별도로 들고 다닌다 (조항 ②).
    undetermined: list[SourceSummary] = field(default_factory=list)

    @property
    def load_paths(self) -> list[Path]:
        return [summary.path for summary in self.add + self.replace]

    def purge_targets(self, scope: str, exclude_periods: frozenset[str] = frozenset()) -> list[PurgeTarget]:
        """교체 대상 목록. ★pinned(제외) 기간은 걷어내지 않는다.

        exclude_periods 는 s1 이 그 기간을 ★재적재하지 않는다는 뜻이다.
        걷어내기만 하고 다시 넣지 않으면 그 기간이 영구히 비므로,
        여기서 먼저 제외한다. 실제로 교체가 요청됐는데 제외된 경우는
        run_incremental_ubist_load 가 ★fail closed 로 막는다.
        """
        targets: list[PurgeTarget] = []
        for summary in self.replace:
            if scope == REPLACE_SCOPE_SOURCE_FILE:
                value = summary.source_file
            elif scope == REPLACE_SCOPE_SOURCE_FOLDER:
                value = summary.source_folder
            else:
                value = ""
            for period in summary.periods:
                if period in exclude_periods:
                    continue
                targets.append(PurgeTarget(period=period, scope=scope, value=value))
        return targets

    def replaced_excluded_periods(self, exclude_periods: frozenset[str]) -> dict[str, list[str]]:
        """교체 요청이 pinned 기간을 건드리려 한 경우를 모은다."""
        hits: dict[str, list[str]] = {}
        for summary in self.replace:
            blocked = sorted(set(summary.periods) & exclude_periods)
            if blocked:
                hits[summary.source_file] = blocked
        return hits


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


def period_replace_enabled() -> bool:
    """기간 단위 교체 사용 여부. 기본 켜짐. 끄는 의미는 상단 상수 주석 참조."""
    return os.environ.get(INGEST_PERIOD_REPLACE_ENV, "1").strip().lower() not in FALSEY


def period_replace_scope() -> str:
    """교체 키. 기본은 형제 파일을 건드리지 않는 (기간, 파일) 조합."""
    scope = os.environ.get(INGEST_PERIOD_REPLACE_SCOPE_ENV, REPLACE_SCOPE_SOURCE_FILE).strip()
    if scope not in REPLACE_SCOPES:
        raise ValueError(
            f"{INGEST_PERIOD_REPLACE_SCOPE_ENV} must be one of {REPLACE_SCOPES}, got {scope!r}"
        )
    return scope


def file_digest(path: Path) -> str:
    """재업로드가 '내용이 바뀐 정정본' 인지 가르는 유일한 근거."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
        workbook = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
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


def build_generic_lookup_from_corpus(target: Path) -> dict[str, str]:
    """Derive the Generic lookup from the already-loaded parquet corpus.

    Generic is not a cell value on most rows; it is derived at load time from
    whichever workbooks happened to be read.  A partial load therefore produces
    a partial lookup, and rows that only carry the core dimensions come out with
    Generic NULL even though the value is known corpus-wide.

    Reading the corpus back closes that hole without re-opening 66 workbooks.
    This mirrors the aggregation the sole consumer already performs
    (io/catalog/postfix/oxgx.py build_ubist_generic_by_brand), so the loader and
    the consumer agree on what "the" Generic for a key is.
    """
    pattern = target / "year=*" / "month=*" / "data.parquet"
    if not any(target.glob("year=*/month=*/data.parquet")):
        return {}
    values = ", ".join(f"'{value}'" for value in sorted(GENERIC_VALUES))
    connection = duckdb.connect(":memory:")
    try:
        connection.execute("SET memory_limit='1GB'")
        connection.execute("SET threads=2")
        connection.execute("SET preserve_insertion_order=false")
        rows = connection.execute(
            f"""
            SELECT "약품코드", "제품", "브랜드", "Generic", count(*) AS cnt
            FROM read_parquet(?, hive_partitioning = true)
            WHERE "Generic" IN ({values})
            GROUP BY 1, 2, 3, 4
            """,
            [str(pattern)],
        ).fetchall()
    finally:
        connection.close()

    counts: dict[str, Counter[str]] = defaultdict(Counter)
    for code, product, brand, generic, count in rows:
        base = {"약품코드": code, "제품": product, "브랜드": brand}
        for key in generic_lookup_keys(base):
            counts[key][str(generic)] += int(count)
    lookup = {key: counter.most_common(1)[0][0] for key, counter in counts.items() if counter}
    LOGGER.info("generic lookup from corpus keys=%s target=%s", f"{len(lookup):,}", target)
    return lookup


def merge_generic_lookups(
    source_lookup: dict[str, str],
    corpus_lookup: dict[str, str],
) -> dict[str, str]:
    """Uploaded workbooks win; the corpus only fills keys they do not cover.

    Precedence matters: a re-upload that genuinely changes a product's Generic
    must take effect, so the freshly read workbooks are authoritative for the
    keys they carry.  The corpus is a backfill, not an override.
    """
    merged = dict(corpus_lookup)
    merged.update(source_lookup)
    return merged


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


def iter_xlsx_rows(xlsx_path: Path, generic_lookup: dict[str, str] | None = None):
    workbook = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
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
                    yield period, output
    finally:
        workbook.close()


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


def purge_partition_rows(target_root: Path, targets: list[PurgeTarget]) -> dict[str, int]:
    """교체 대상 행을 파티션에서 걷어낸다.

    ★duckdb 로 streaming 처리한다. deduplicate_partition_file 과 같은 이유다 —
    파티션 하나가 200만 행을 넘고, pandas 로 전량을 올리면 전량 리허설에서
    메모리 상한을 넘긴다.  read_parquet → COPY 로 흘려보내므로 파티션이 통째로
    메모리에 올라오지 않는다.

    호출자는 ★tmp 사본 위에서 이 함수를 부른다. 원본 target 은 건드리지 않으며,
    tmp 전체가 원자 rename 으로 교체되므로 중간 실패 시 기존 데이터가 그대로 남는다.
    파티션 파일 자체도 임시 파일에 쓴 뒤 replace 하여 부분 기록 상태를 남기지 않는다.
    """
    removed: dict[str, int] = {}
    if not targets:
        return removed

    by_period: dict[str, list[PurgeTarget]] = defaultdict(list)
    for target in targets:
        by_period[target.period].append(target)

    quoted_columns = ", ".join(f'"{column}"' for column in COLUMNS)

    for period in sorted(by_period):
        year, month = period.split("-")
        path = target_root / f"year={year}" / f"month={month}" / "data.parquet"
        if not path.exists():
            # 교체 대상 기간이 아직 없으면 걷어낼 것도 없다. 신규 적재와 같다.
            removed[period] = 0
            continue

        group = by_period[period]
        scopes = {target.scope for target in group}
        if REPLACE_SCOPE_PERIOD in scopes:
            predicate = "TRUE"
            params: list[str] = []
        else:
            clauses = []
            params = []
            for target in group:
                column = "source_file" if target.scope == REPLACE_SCOPE_SOURCE_FILE else "source_folder"
                # NFC 로 맞춰 비교한다. 파일명이 NFD 로 들어오는 경로가 있다.
                clauses.append(f'nfc_normalize("{column}") = nfc_normalize(?)')
                params.append(unicodedata.normalize("NFC", target.value))
            predicate = " OR ".join(clauses)

        temp_path = path.with_suffix(".purge.tmp")
        with tempfile.TemporaryDirectory(prefix="ubist-purge-", dir=path.parent) as work_dir_name:
            work_dir = Path(work_dir_name)
            spill_dir = work_dir / "spill"
            spill_dir.mkdir()
            connection = duckdb.connect(str(work_dir / "purge.duckdb"))
            try:
                connection.execute("SET memory_limit='4GB'")
                connection.execute("SET threads=2")
                connection.execute("SET preserve_insertion_order=false")
                connection.execute("SET temp_directory=?", [str(spill_dir)])
                before, matched = connection.execute(
                    f"SELECT count(*), coalesce(sum(CASE WHEN {predicate} THEN 1 ELSE 0 END), 0) "
                    f"FROM read_parquet(?)",
                    [*params, str(path)] if params else [str(path)],
                ).fetchone()
                temp_sql = str(temp_path).replace("'", "''")
                connection.execute(
                    f"""
                    COPY (
                      SELECT {quoted_columns}
                      FROM read_parquet(?)
                      WHERE NOT ({predicate})
                    ) TO '{temp_sql}' (FORMAT PARQUET, COMPRESSION SNAPPY)
                    """,
                    [str(path), *params] if params else [str(path)],
                )
            finally:
                connection.close()

        temp_path.replace(path)
        removed[period] = int(matched)
        LOGGER.info(
            "UBIST period replace purge period=%s scope=%s rows_before=%s rows_removed=%s rows_kept=%s",
            period,
            ",".join(sorted(scopes)),
            f"{int(before):,}",
            f"{int(matched):,}",
            f"{int(before) - int(matched):,}",
        )
    return removed


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
    purge_targets: list[PurgeTarget] | None = None,
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

    # ★ 교체는 tmp 사본 위에서, ★새 행을 쓰기 전에 끝내야 한다.
    #   PartitionWriter._open_writer 가 기존 파티션을 읽어 새 writer 로 옮기므로
    #   그 뒤에 지우면 방금 쓴 새 행까지 함께 지워진다.
    purged = purge_partition_rows(tmp_target, purge_targets or [])

    buffers: dict[str, list[dict[str, object]]] = defaultdict(list)
    writer = PartitionWriter(tmp_target)
    total_rows = 0
    # 업로드된 워크북이 우선, 이미 적재된 코퍼스가 빈 키를 메운다.
    # 부분 적재에서 Generic 이 NULL 로 떨어지는 것을 막는다 (io/catalog/postfix/oxgx.py 가
    # 이 열로 ml_006/007/008 의 ox_gx 를 정한다).
    generic_lookup = merge_generic_lookups(
        build_generic_lookup(xlsx_paths),
        build_generic_lookup_from_corpus(tmp_target),
    )
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
    # 교체했는데 새 파일이 그 기간에 한 행도 주지 않은 경우에도 manifest 가 옛 row_count 를
    # 들고 있으면 안 된다. 걷어낸 기간은 전부 실제 파일에서 다시 세어 stats 에 넣는다.
    for period in purged:
        if period not in writer.stats:
            writer.stats[period] = PartitionStats()
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

    # 재업로드가 정정본인지 여부는 ★파일명이 아니라 내용 digest 로만 가를 수 있다.
    digests: dict[str, str] = {}
    if previous_manifest:
        recorded = previous_manifest.get("source_file_digests")
        if isinstance(recorded, dict):
            digests.update({str(name): str(value) for name, value in recorded.items()})
    for path in xlsx_paths:
        digests[normalized_source_file(path)] = file_digest(path)

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
        "source_file_digests": dict(sorted(digests.items())),
        "period_replace_policy": {
            "enabled": period_replace_enabled(),
            "scope": period_replace_scope(),
            "decision": "same name + different digest = replace that upload's share of the period",
        },
        "partitions": [partition_entries[period] for period in sorted(partition_entries)],
    }
    target.mkdir(parents=True, exist_ok=True)
    (target / "_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def dry_run(xlsx_paths: list[Path], limit_rows: int = 3) -> None:
    print("# UBIST Parquet Loader Dry Run\n")
    for xlsx_path in xlsx_paths:
        print(f"## Source: {xlsx_path}\n")
        workbook = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
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
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
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
        digest=file_digest(path),
    )


def manifest_source_digests(manifest: dict[str, object]) -> dict[str, str]:
    recorded = manifest.get("source_file_digests")
    if not isinstance(recorded, dict):
        return {}
    return {unicodedata.normalize("NFC", str(name)): str(value) for name, value in recorded.items()}


def existing_partition_for(target: Path, period: str) -> Path:
    year, month = period.split("-")
    return target / f"year={year}" / f"month={month}" / "data.parquet"


def source_period_frame(path: Path, period: str) -> pd.DataFrame:
    rows = [row for row_period, row in iter_xlsx_rows(path) if row_period == period]
    return prepare_frame(rows)


def source_period_frames(path: Path, periods: set[str]) -> dict[str, pd.DataFrame]:
    rows_by_period: dict[str, list[dict[str, object]]] = {period: [] for period in periods}
    for row_period, row in iter_xlsx_rows(path):
        if row_period in rows_by_period:
            rows_by_period[row_period].append(row)
    return {period: prepare_frame(rows) for period, rows in rows_by_period.items()}


def incremental_plan(
    xlsx_paths: list[Path],
    target: Path,
    *,
    replace_enabled: bool | None = None,
) -> IncrementalPlan:
    manifest = read_manifest(target)
    loaded = manifest_source_files(manifest)
    digests = manifest_source_digests(manifest)
    summaries = [summarize_source(path) for path in xlsx_paths]
    if replace_enabled is None:
        replace_enabled = period_replace_enabled()

    add = [summary for summary in summaries if summary.source_file not in loaded]
    reloaded = [summary for summary in summaries if summary.source_file in loaded]

    skip: list[SourceSummary] = []
    replace: list[SourceSummary] = []
    undetermined: list[SourceSummary] = []
    for summary in reloaded:
        if not replace_enabled:
            skip.append(summary)
            continue
        recorded = digests.get(summary.source_file)
        if recorded is None:
            # digest 가 없는 구 manifest. 정정본인지 ★알 수 없다.
            # 모르는 것을 "동일" 로 적으면 조항 ② 위반이므로 별도로 분리해 경고한다.
            undetermined.append(summary)
            skip.append(summary)
            LOGGER.warning(
                "UBIST reupload digest unknown source_file=%s: manifest has no digest, "
                "cannot tell correction from duplicate; skipped without replacing",
                summary.source_file,
            )
            continue
        if recorded == summary.digest:
            skip.append(summary)
            continue
        replace.append(summary)
        LOGGER.warning(
            "UBIST reupload detected source_file=%s periods=%s digest %s -> %s: "
            "replacing this upload's share of those periods",
            summary.source_file,
            ",".join(summary.periods),
            recorded[:12],
            summary.digest[:12],
        )

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
    # 충돌 판정은 ★신규 파일에만 건다. 교체 대상은 옛 몫을 먼저 걷어내므로
    # 자기 자신과의 겹침이 당연하고, 그것을 충돌로 부르면 교체가 매번 막힌다.
    conflicts = detect_period_conflicts(add, existing)
    conflicts.extend(detect_content_overlaps(add, target))
    return IncrementalPlan(
        add=add,
        skip=skip,
        conflicts=conflicts,
        loaded_source_files=loaded,
        replace=replace,
        undetermined=undetermined,
    )


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
    print(f"- period replacements: {len(plan.replace)}")
    print(f"- skipped already loaded: {len(plan.skip)}")
    print(f"- undetermined (no digest in manifest): {len(plan.undetermined)}")
    print(f"- conflicts: {len(plan.conflicts)}\n")

    print("## ADD")
    for summary in plan.add:
        print(f"- {summary.path} | periods={summary.periods[0]}..{summary.periods[-1]} ({len(summary.periods)})")
    if plan.replace:
        print("\n## REPLACE (same name, different content)")
        for summary in plan.replace:
            print(f"- {summary.source_file} | periods={','.join(summary.periods)} | digest={summary.digest[:12]}")
    if plan.undetermined:
        print("\n## UNDETERMINED (manifest has no digest — correction cannot be detected)")
        for summary in plan.undetermined:
            print(f"- {summary.source_file}")
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
    exclude_periods: frozenset[str] = frozenset(),
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
    replace_enabled = period_replace_enabled()
    scope = period_replace_scope() if replace_enabled else REPLACE_SCOPE_SOURCE_FILE
    plan = incremental_plan(xlsx_paths, target, replace_enabled=replace_enabled)
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

    # ★ pinned(제외) 기간을 교체하려 하면 fail closed.
    #   제외된 기간은 s1 이 ★다시 쓰지 않는다. 걷어내기만 하면 영구 결손이 된다.
    #   조용히 건너뛰지 않고 멈춘다 — 무엇이 반영되지 않았는지 모르는 편이 더 나쁘다.
    if replace_enabled and exclude_periods:
        blocked = plan.replaced_excluded_periods(exclude_periods)
        if blocked:
            raise RuntimeError(
                "UBIST period replace targets pinned periods that s1 will not reload: "
                f"{blocked}; unpin them or exclude the file from this run"
            )

    load_paths = plan.load_paths
    if not load_paths:
        LOGGER.info("UBIST incremental load has no new or changed source files target=%s", target)
        return {}
    purge_targets = plan.purge_targets(scope, exclude_periods) if replace_enabled else []
    if scope == REPLACE_SCOPE_PERIOD and purge_targets:
        LOGGER.warning(
            "UBIST period replace scope=period will drop ★all source files' rows in periods=%s",
            ",".join(sorted({item.period for item in purge_targets})),
        )
    # 넘길 내용이 없는 인자는 아예 넘기지 않는다. 교체도 pinned 도 없는 경로의
    # 호출 형태를 예전 그대로 두어 기존 호출자·테스트 더블과의 계약을 깨지 않는다.
    extra: dict[str, object] = {}
    if purge_targets:
        extra["purge_targets"] = purge_targets
    if exclude_periods:
        extra["exclude_periods"] = exclude_periods
    return load_to_parquet(
        load_paths,
        target,
        mode="append",
        truncate=True,
        previous_manifest=manifest,
        **extra,
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
