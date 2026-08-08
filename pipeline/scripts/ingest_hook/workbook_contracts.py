"""Category workbook summaries shared by G3 and isolated staging loads."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WorkbookSummary:
    rows: int | None
    periods: frozenset[str]
    detail: str


def summarize(category: str, path: Path, epoch: str) -> WorkbookSummary:
    readers = {
        "ubist": _ubist,
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


def summarize_inventory(category: str, path: Path, epoch: str) -> WorkbookSummary:
    """Read only the headers needed for inventory period selection when possible."""
    if category == "ubist":
        from pipeline.etl.io.ubist_loader import summarize_source

        summary = summarize_source(path)
        if not summary.periods:
            raise ValueError("UBIST workbook has no parseable metric periods")
        return WorkbookSummary(
            None,
            frozenset(summary.periods),
            "ubist_loader.summarize_source(header-only)",
        )
    if category == "iqvia_nsa":
        return _nsa_inventory(path, epoch)
    return summarize(category, path, epoch)


def _nsa_inventory(path: Path, epoch: str) -> WorkbookSummary:
    import openpyxl

    from pipeline.etl.io.iqvia_loader import canonicalize_nsa_headers, nsa_period_columns

    periods: set[str] = set()
    long_format = False
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        for worksheet in workbook.worksheets:
            rows = worksheet.iter_rows(values_only=True)
            try:
                raw_headers = next(rows)
            except StopIteration:
                continue
            headers = canonicalize_nsa_headers(
                raw_headers,
                source=f"{path}:{worksheet.title}",
            )
            month_periods = nsa_period_columns(headers)
            if not month_periods:
                long_format = True
                continue
            for month_period in month_periods:
                year, month = month_period.split("-", 1)
                quarter = (int(month) - 1) // 3 + 1
                periods.add(f"{year}-Q{quarter}")
    finally:
        workbook.close()
    if long_format:
        # Long-format period values live in rows; retain the exact reader for
        # that uncommon contract rather than infer periods from filenames.
        return _nsa(path, epoch)
    if not periods:
        raise ValueError("IQVIA NSA workbook has no parseable period headers")
    return WorkbookSummary(
        None,
        frozenset(periods),
        "iqvia_loader canonical header period scan",
    )


def _ubist(path: Path, _epoch: str) -> WorkbookSummary:
    from pipeline.etl.io.ubist_loader import count_source_rows_by_period

    counts = count_source_rows_by_period(path)
    row_count = sum(counts.values())
    if row_count == 0:
        raise ValueError("UBIST workbook has no parseable metric rows")
    return WorkbookSummary(
        row_count,
        frozenset(counts),
        "ubist_loader.count_source_rows_by_period",
    )


def _nsa(path: Path, _epoch: str) -> WorkbookSummary:
    from pipeline.etl.io.iqvia_loader import iter_nsa_xlsx

    row_count = 0
    periods: set[str] = set()
    for row in iter_nsa_xlsx(path):
        row_count += 1
        periods.add(f"{int(row['period_yyyy']):04d}-Q{int(row['period_quarter'])}")
    if row_count == 0:
        raise ValueError("IQVIA NSA workbook has no parseable metric rows")
    return WorkbookSummary(row_count, frozenset(periods), "iqvia_loader.iter_nsa_xlsx")


def _csd(path: Path, _epoch: str) -> WorkbookSummary:
    from pipeline.scripts.etl.brand_activity.csd_core import discover_market_sheets, iter_market_rows

    sheets = discover_market_sheets(path)
    if not sheets:
        raise ValueError("CSD workbook has no sheet with canonical headers")
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
