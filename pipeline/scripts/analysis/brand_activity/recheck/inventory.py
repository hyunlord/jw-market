from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Final, Sequence

import openpyxl


MONTHS: Final[dict[str, int]] = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
MONTH_RE: Final[re.Pattern[str]] = re.compile(
    r"(?<![A-Za-z])(jan|feb|mar|apr|may|jun|june|jul|july|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s*(\d{2}|\d{4})(?!\d)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class FileRecord:
    kind: str
    file_name: str
    path: Path
    bytes: int
    sha256: str
    month_ym: str | None
    sheet_names: tuple[str, ...]

    def to_json(self) -> dict[str, object]:
        """Return stable JSON lineage for one discovered source file."""
        row = asdict(self)
        row["path"] = str(self.path)
        row["sheet_names"] = list(self.sheet_names)
        return row


def month_from_filename(file_name: str) -> str | None:
    """Infer a source month from observed IQVIA workbook filename variants."""
    match = MONTH_RE.search(file_name)
    if match is None:
        return None
    month = MONTHS[match.group(1).lower()]
    year_raw = int(match.group(2))
    year = 2000 + year_raw if year_raw < 100 else year_raw
    return f"{year:04d}-{month:02d}"


def infer_folder_kind(path: Path) -> str:
    """Classify source files by folder and filename, never by workbook content."""
    text = " ".join([part.lower() for part in path.parts] + [path.name.lower()])
    if "meeting" in text:
        return "meeting"
    if "keyword" in text:
        return "keyword"
    if "channeldynamics" in text or path.name.lower().startswith("csd_"):
        return "csd"
    return "unknown"


def sha256_file(path: Path) -> str:
    """Hash a local source file for current-vs-previous manifest comparison."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def workbook_sheets(path: Path) -> tuple[str, ...]:
    """Read workbook sheet names for inventory without extracting source rows."""
    if path.suffix.lower() != ".xlsx":
        return ()
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        return tuple(workbook.sheetnames)
    finally:
        workbook.close()


def scan_source_roots(roots: Sequence[Path]) -> tuple[list[FileRecord], list[str]]:
    """Scan source roots recursively and record missing roots as audit evidence."""
    records: list[FileRecord] = []
    missing_roots: list[str] = []
    for root in roots:
        expanded = root.expanduser()
        if not expanded.exists():
            missing_roots.append(str(expanded))
            continue
        for path in sorted(expanded.rglob("*")):
            if path.name.startswith("~$") or path.suffix.lower() not in {".xlsx", ".csv"}:
                continue
            records.append(
                FileRecord(
                    kind=infer_folder_kind(path),
                    file_name=path.name,
                    path=path,
                    bytes=path.stat().st_size,
                    sha256=sha256_file(path),
                    month_ym=month_from_filename(path.name),
                    sheet_names=workbook_sheets(path),
                )
            )
    return sorted(records, key=lambda row: (row.kind, row.month_ym or "", row.file_name)), missing_roots


def load_manifest_records(path: Path) -> list[FileRecord]:
    """Load prior source manifests into the same shape as the current scan."""
    if not path.exists():
        return []
    rows = json.loads(path.read_text(encoding="utf-8"))
    records: list[FileRecord] = []
    for row in rows:
        source_path = Path(str(row.get("path") or row.get("file") or ""))
        file_name = str(row.get("file") or source_path.name)
        records.append(
            FileRecord(
                kind=infer_folder_kind(source_path),
                file_name=file_name,
                path=source_path,
                bytes=int(row.get("bytes") or 0),
                sha256=str(row["sha256"]),
                month_ym=str(row.get("source_period_ym") or month_from_filename(file_name) or "") or None,
                sheet_names=tuple(row.get("sheet_names") or ()),
            )
        )
    return records


def compare_manifests(previous: Sequence[FileRecord], current: Sequence[FileRecord]) -> dict[str, list[FileRecord]]:
    """Compare manifests by file name and SHA256 to find source churn."""
    previous_by_file = {row.file_name: row for row in previous}
    current_by_file = {row.file_name: row for row in current}
    current_files = set(current_by_file)
    previous_files = set(previous_by_file)
    unchanged = [
        current_by_file[name]
        for name in sorted(current_files & previous_files)
        if current_by_file[name].sha256 == previous_by_file[name].sha256
    ]
    changed = [
        current_by_file[name]
        for name in sorted(current_files & previous_files)
        if current_by_file[name].sha256 != previous_by_file[name].sha256
    ]
    return {
        "new": [current_by_file[name] for name in sorted(current_files - previous_files)],
        "changed": changed,
        "deleted": [previous_by_file[name] for name in sorted(previous_files - current_files)],
        "unchanged": unchanged,
    }


def month_coverage(records: Sequence[FileRecord]) -> dict[str, list[str]]:
    """Return discovered source-month coverage by CSD/Keyword/Meeting kind."""
    coverage: dict[str, set[str]] = {"csd": set(), "keyword": set(), "meeting": set(), "unknown": set()}
    for record in records:
        if record.month_ym:
            coverage.setdefault(record.kind, set()).add(record.month_ym)
    return {kind: sorted(months) for kind, months in sorted(coverage.items()) if months}


def write_records(path: Path, records: Sequence[FileRecord]) -> None:
    """Write current inventory records as deterministic UTF-8 JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [record.to_json() for record in records]
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
