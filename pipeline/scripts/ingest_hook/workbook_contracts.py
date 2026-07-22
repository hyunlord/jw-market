"""Category workbook summaries shared by G3 and isolated staging loads."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WorkbookSummary:
    rows: int
    periods: frozenset[str]
    detail: str


def summarize(category: str, path: Path, epoch: str) -> WorkbookSummary:
    readers = {
        "iqvia_nsa": _nsa,
        "iqvia_csd_channel": _csd,
        "iqvia_csd_keyword": _keyword,
        "mi_master": _mi_master,
    }
    try:
        reader = readers[category]
    except KeyError as exc:
        raise ValueError(f"no workbook contract for category {category!r}") from exc
    return reader(path, epoch)


def _nsa(path: Path, _epoch: str) -> WorkbookSummary:
    from pipeline.etl.io.iqvia_loader import iter_nsa_xlsx

    rows = list(iter_nsa_xlsx(path))
    if not rows:
        raise ValueError("IQVIA NSA workbook has no parseable metric rows")
    periods = frozenset(
        f"{int(row['period_yyyy']):04d}-Q{int(row['period_quarter'])}" for row in rows
    )
    return WorkbookSummary(len(rows), periods, "iqvia_loader.iter_nsa_xlsx")


def _csd(path: Path, _epoch: str) -> WorkbookSummary:
    from pipeline.scripts.etl.brand_activity.csd_core import iter_market_rows, select_market_sheets

    import openpyxl

    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        sheets = select_market_sheets(tuple(workbook.sheetnames))
    finally:
        workbook.close()
    if not sheets:
        raise ValueError("CSD workbook has no canonical '* Market' sheet")
    rows = [row for sheet in sheets for row in iter_market_rows(path, sheet)]
    if not rows:
        raise ValueError("CSD workbook has no TOTAL-region rows")
    return WorkbookSummary(len(rows), frozenset(row.period_ym for row in rows), "csd_core.iter_market_rows")


def _keyword(path: Path, _epoch: str) -> WorkbookSummary:
    from pipeline.scripts.etl.brand_activity.ingest_keyword import read_keyword_events

    rows = read_keyword_events(path)
    if not rows:
        raise ValueError("Keyword workbook has no event rows")
    return WorkbookSummary(len(rows), frozenset(row.period_ym for row in rows), "ingest_keyword.read_keyword_events")


def _mi_master(path: Path, epoch: str) -> WorkbookSummary:
    from tempfile import TemporaryDirectory

    from pipeline.etl.io.catalog.master.extracts import run_master_extracts

    # G3 executes the canonical five MI Master extractors and their validators.
    # The temporary output root is discarded; no catalog or DB path is touched.
    with TemporaryDirectory(prefix="ingest-mi-master-g3-") as temp_root:
        results = run_master_extracts(output_root=Path(temp_root), input_file=path)
    rows = sum(item.rows for item in results)
    if rows <= 0:
        raise ValueError("MI Master extractors produced no rows")
    detail = "MI Master canonical extractors: " + ",".join(item.name for item in results)
    return WorkbookSummary(rows, frozenset({epoch}), detail)
