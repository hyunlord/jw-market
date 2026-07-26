"""Source workbook discovery and raw CSD extraction for brand activity."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from pipeline.scripts.etl.brand_activity.csd_core import (
    CsdRow,
    _header_index,
    is_total_region,
    normalize_text,
    parse_period_ym,
    parse_product_details,
    source_month_key,
)
from pipeline.scripts.etl.brand_activity.km_core import JsonValue, source_period_from_name
from pipeline.scripts.ingest_hook.source_fingerprint import (
    matching_sheets,
    open_workbook_by_content,
)

KEYWORD_WORKBOOK_PATTERN = "*Keywords for JW*.xlsx"


@dataclass(frozen=True, slots=True)
class SourceRoots:
    """Resolved source folders for the reorganized CSD and Keyword files."""

    csd: Path
    keyword: Path


@dataclass(frozen=True, slots=True)
class CsdSourceRow:
    """Normalized CSD workbook row with source provenance."""

    source_file: str
    source_file_sha256: str
    source_sheet: str
    source_row_no: int
    source_period_ym: str
    period_ym: str
    market: str
    jw_channel: str
    region: str
    master_product: str
    manufacturer: str
    representing_company: str
    product_details: int
    selected_for_stage: bool

    def to_json(self) -> dict[str, JsonValue]:
        """Serialize the source row for raw payload storage."""
        return asdict(self)

    def to_stage_row(self) -> CsdRow:
        """Convert a selected TOTAL-region CSD row to the legacy stage shape."""
        return CsdRow(
            source_file=self.source_file,
            source_sheet=self.source_sheet,
            source_row_no=self.source_row_no,
            period_ym=self.period_ym,
            market=self.market,
            jw_channel=self.jw_channel,
            master_product=self.master_product,
            representing_company=self.representing_company,
            product_details=self.product_details,
        )


def resolve_source_roots(root: Path) -> SourceRoots:
    """Find the CSD and Keyword source folders under `data/IQVIA/CSD`."""
    directories = [path for path in root.iterdir() if path.is_dir()]
    csd = _single_dir(directories, "ChannelDynamics")
    keyword = _single_dir(directories, "Keyword")
    return SourceRoots(csd=csd, keyword=keyword)


def discover_source_files(roots: SourceRoots) -> dict[str, list[Path]]:
    """Return sorted source workbooks, excluding Excel lock files."""
    return {
        "csd": discover_csd_source_files(roots),
        "keyword": discover_keyword_source_files(roots),
    }


def discover_csd_source_files(roots: SourceRoots) -> list[Path]:
    """Return CSD source workbooks without scanning Keyword folders."""
    return _sorted_workbooks(roots.csd, "ChannelDynamics*.xlsx")


def discover_keyword_source_files(roots: SourceRoots) -> list[Path]:
    """Return Keyword source workbooks without scanning CSD folders."""
    return _sorted_workbooks(roots.keyword, KEYWORD_WORKBOOK_PATTERN)


def read_csd_source_rows(workbook_path: Path, workbook_hash: str) -> list[CsdSourceRow]:
    """Read CSD market sheets, preserving non-TOTAL rows in raw staging."""
    candidates = matching_sheets(workbook_path, "iqvia_csd_channel")
    if not candidates:
        raise ValueError("CSD workbook has no content-matching source sheets")
    workbook = open_workbook_by_content(workbook_path)
    try:
        rows: list[CsdSourceRow] = []
        for candidate in candidates:
            sheet = workbook[candidate.sheet_name]
            header = next(
                sheet.iter_rows(
                    min_row=candidate.header_row_no,
                    max_row=candidate.header_row_no,
                    values_only=True,
                )
            )
            indexes = _header_index(header)
            for source_row_no, values in enumerate(
                sheet.iter_rows(
                    min_row=candidate.header_row_no + 1,
                    values_only=True,
                ),
                start=candidate.header_row_no + 1,
            ):
                if not any(normalize_text(value) for value in values):
                    continue
                product_details = parse_product_details(values[indexes["Product Details"]])
                region = normalize_text(values[indexes["Region"]])
                period_ym = parse_period_ym(values[indexes["Related date"]])
                rows.append(
                    CsdSourceRow(
                        source_file=workbook_path.name,
                        source_file_sha256=workbook_hash,
                        source_sheet=candidate.sheet_name,
                        source_row_no=source_row_no,
                        source_period_ym=period_ym,
                        period_ym=period_ym,
                        market=normalize_text(values[indexes["Market"]]),
                        jw_channel=normalize_text(values[indexes["JW Channel"]]),
                        region=region,
                        master_product=normalize_text(values[indexes["Master product"]]),
                        manufacturer=normalize_text(values[indexes["Manufacturer"]]),
                        representing_company=normalize_text(values[indexes["Representing Company"]]),
                        product_details=product_details,
                        selected_for_stage=is_total_region(region),
                    )
                )
        return rows
    finally:
        workbook.close()


def _single_dir(directories: list[Path], name_part: str) -> Path:
    """Find exactly one source subdirectory by visible name fragment."""
    matches = [path for path in directories if name_part in path.name]
    if len(matches) != 1:
        raise FileNotFoundError(f"Expected one {name_part!r} folder, found {len(matches)}")
    return matches[0]


def _sorted_workbooks(root: Path, pattern: str) -> list[Path]:
    """Sort source workbooks by parsed period and name."""
    files = [path for path in root.glob(pattern) if not path.name.startswith("~$")]
    return sorted(files, key=_source_sort_key)


def _source_sort_key(path: Path) -> tuple[str, str]:
    """Return a comparable source period/name pair for any source file."""
    if "ChannelDynamics" in path.name:
        return (_source_period_for_csd(path), path.name)
    return (source_period_from_name(path), path.name)


def _source_period_for_csd(path: Path) -> str:
    """Parse CSD source period from the report filename."""
    year, month, _ = source_month_key(path.name)
    if year == 0 or month == 0:
        raise ValueError(f"CSD source filename has no month: {path.name}")
    return f"{year:04d}-{month:02d}"
