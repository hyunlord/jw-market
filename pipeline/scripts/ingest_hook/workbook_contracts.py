"""Category workbook summaries shared by G3 and isolated staging loads."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WorkbookSummary:
    rows: int
    periods: frozenset[str]
    detail: str


class WorkbookClassificationError(ValueError):
    """Raised when workbook content matches zero or multiple source contracts."""


def classify(path: Path, epoch: str) -> str:
    """Return one content-derived category without materializing every parser."""
    matches: list[str] = []
    errors: dict[str, str] = {}
    for category in (
        "ubist",
        "iqvia_nsa",
        "iqvia_csd_channel",
        "iqvia_csd_keyword",
    ):
        try:
            _matches_structure(category, path)
        except Exception as exc:  # each parser is an independent candidate
            errors[category] = f"{type(exc).__name__}: {exc}"
        else:
            matches.append(category)
    if not matches:
        # MI Master has several heterogeneous sheet contracts. Run its canonical
        # extractor only after the four cheap header fingerprints reject, so an
        # NSA/CSD upload never pays for a full MI catalog extraction.
        try:
            _mi_master(path, epoch)
        except Exception as exc:
            errors["mi_master"] = f"{type(exc).__name__}: {exc}"
        else:
            matches.append("mi_master")
    if len(matches) != 1:
        detail = "; ".join(f"{key}={value}" for key, value in errors.items())
        if not matches:
            raise WorkbookClassificationError(
                f"workbook content matches 0 categories; {detail}"
            )
        raise WorkbookClassificationError(
            f"workbook content is ambiguous across categories {matches}"
        )
    return matches[0]


def _matches_structure(category: str, path: Path) -> None:
    if category == "ubist":
        from pipeline.etl.io.ubist_loader import summarize_source

        if not summarize_source(path).periods:
            raise ValueError("UBIST workbook has no metric periods")
        return
    from pipeline.scripts.ingest_hook.source_fingerprint import (
        single_matching_sheet,
    )

    single_matching_sheet(path, category)


def summarize(category: str, path: Path, epoch: str) -> WorkbookSummary:
    return _summary_for(category, path, epoch)


def _summary_for(category: str, path: Path, epoch: str) -> WorkbookSummary:
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


def _ubist(path: Path, _epoch: str) -> WorkbookSummary:
    from pipeline.etl.io.ubist_loader import summarize_source

    summary = summarize_source(path)
    if not summary.periods:
        raise ValueError("UBIST workbook has no metric periods")
    return WorkbookSummary(
        1,
        frozenset(summary.periods),
        "ubist_loader.summarize_source",
    )


def _nsa(path: Path, _epoch: str) -> WorkbookSummary:
    from pipeline.scripts.ingest_hook.source_fingerprint import fingerprint_source

    fingerprint = fingerprint_source(path, "iqvia_nsa")
    if fingerprint.natural_key_count <= 0:
        raise ValueError("IQVIA NSA workbook has no parseable metric rows")
    return WorkbookSummary(
        fingerprint.natural_key_count,
        fingerprint.periods,
        "iqvia_loader.iter_nsa_xlsx",
    )


def _csd(path: Path, _epoch: str) -> WorkbookSummary:
    from pipeline.scripts.ingest_hook.source_fingerprint import fingerprint_source

    fingerprint = fingerprint_source(path, "iqvia_csd_channel")
    if fingerprint.natural_key_count <= 0:
        raise ValueError("CSD workbook has no TOTAL-region rows")
    return WorkbookSummary(
        fingerprint.natural_key_count,
        fingerprint.periods,
        "csd_core.iter_market_rows",
    )


def _keyword(path: Path, _epoch: str) -> WorkbookSummary:
    from pipeline.scripts.ingest_hook.source_fingerprint import fingerprint_source

    fingerprint = fingerprint_source(path, "iqvia_csd_keyword")
    if fingerprint.natural_key_count <= 0:
        raise ValueError("Keyword workbook has no event rows")
    return WorkbookSummary(
        fingerprint.natural_key_count,
        fingerprint.periods,
        "ingest_keyword.read_keyword_events",
    )


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
