"""Content-derived workbook fingerprints for ingest-hook source validation."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Final, Iterable, Sequence

from pipeline.etl.io.source_headers import normalize_source_header
from pipeline.etl.io.workbook_content import load_workbook_by_content


HEADER_SCAN_ROWS: Final[int] = 12

CSD_HEADERS: Final[tuple[str, ...]] = tuple(
    "Related date|Market|JW Channel|Region|Master product|Manufacturer|"
    "Representing Company|Product Details".split("|")
)
KEYWORD_HEADERS: Final[tuple[str, ...]] = tuple(
    "Related date|VISIT LOCATION|SPECIALTY NAME|REP# CO|PRODUCT NAME|THERAPEUTIC CLASS|KEYWORDS|INTEREST|"
    "Prescription frequency|Prescription evolution|Abstract and clinical literature / data|"
    "Patient educational literature|Promotional product literature|SAMPLES LEFT|"
    "OTHER MATERIALS LEFT|WHAT OTHER MATERIALS|OTHER COMMENTS".split("|")
)
NSA_REQUIRED_HEADERS: Final[tuple[str, ...]] = tuple("AUDIT CODE|MFR CODE|PRODUCT NAME|PACK DESC".split("|"))
NSA_METRIC_HEADERS: Final[tuple[str, ...]] = tuple("Values LC|Units|Counting Units|Dosage Units|Price".split("|"))
FORBIDDEN_HEADERS: Final[dict[str, tuple[str, ...]]] = {
    "iqvia_csd_channel": ("AUDIT CODE", "KEYWORDS", "VISIT LOCATION"),
    "iqvia_csd_keyword": ("AUDIT CODE", "JW Channel", "Region"),
    "iqvia_nsa": ("Related date", "KEYWORDS", "JW Channel"),
}
MONTH_ALIASES: Final[tuple[tuple[int, tuple[str, ...]], ...]] = (
    (1, ("jan", "january")), (2, ("feb", "february")), (3, ("mar", "march")), (4, ("apr", "april")),
    (5, ("may",)), (6, ("jun", "june")), (7, ("jul", "july")), (8, ("aug", "august")),
    (9, ("sep", "sept", "september")), (10, ("oct", "october")), (11, ("nov", "november")), (12, ("dec", "december")),
)
MONTHS: Final[dict[str, int]] = {name: month for month, names in MONTH_ALIASES for name in names}


class SourceFingerprintError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SourceCandidate:
    category: str
    sheet_name: str
    header_row_no: int
    header_keys: frozenset[str]
    indexes: dict[str, int]


@dataclass(frozen=True, slots=True)
class SourceFingerprint:
    category: str
    sheet_name: str
    header_row_no: int
    periods: frozenset[str]
    natural_keys: frozenset[tuple[str, ...]]
    natural_key_count: int
    natural_key_hash: str
    identity: str


@dataclass(frozen=True, slots=True)
class NaturalKeyDigest:
    periods: frozenset[str]
    row_count: int
    key_hash: str


def normalize_header(value: object) -> str | None:
    if value is None:
        return None
    text = unicodedata.normalize("NFKC", str(value)).replace("\ufeff", "")
    text = unicodedata.normalize("NFC", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text.casefold() or None


def normalize_cell(value: object) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFC", str(value))
    return re.sub(r"\s+", " ", text).strip()


def open_workbook_by_content(path: Path):
    return load_workbook_by_content(path, read_only=True, data_only=True)


def matching_sheets(path: Path, category: str) -> tuple[SourceCandidate, ...]:
    workbook = open_workbook_by_content(path)
    try:
        matches: list[SourceCandidate] = []
        for sheet in workbook.worksheets:
            for row_no, row in enumerate(
                sheet.iter_rows(min_row=1, max_row=HEADER_SCAN_ROWS, values_only=True),
                start=1,
            ):
                candidate = _candidate_from_header(category, sheet.title, row_no, row)
                if candidate is not None:
                    matches.append(candidate)
                    break
        return tuple(matches)
    finally:
        workbook.close()


def single_matching_sheet(path: Path, category: str) -> SourceCandidate:
    matches = matching_sheets(path, category)
    if not matches:
        raise SourceFingerprintError(f"0 matching workbook structures for {category}: missing required headers")
    if len(matches) > 1:
        names = [match.sheet_name for match in matches]
        raise SourceFingerprintError(f"multiple matching workbook structures for {category}: {names}")
    return matches[0]


def fingerprint_source(path: Path, category: str) -> SourceFingerprint:
    candidates = matching_sheets(path, category)
    if not candidates:
        raise SourceFingerprintError(
            f"0 matching workbook structures for {category}: missing required headers"
        )
    if category != "iqvia_csd_channel" and len(candidates) > 1:
        names = [candidate.sheet_name for candidate in candidates]
        raise SourceFingerprintError(
            f"multiple matching workbook structures for {category}: {names}"
        )
    digest = _natural_key_digest(path, candidates)
    if digest.row_count == 0:
        raise SourceFingerprintError(f"missing natural keys for {category}")
    required_headers = _required_headers(category)
    payload = {
        "category": category,
        "headers": sorted(required_headers),
        "natural_key_count": digest.row_count,
        "natural_key_hash": digest.key_hash,
        "periods": sorted(digest.periods),
    }
    identity = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return SourceFingerprint(
        category=category,
        sheet_name=",".join(candidate.sheet_name for candidate in candidates),
        header_row_no=candidates[0].header_row_no,
        periods=digest.periods,
        natural_keys=frozenset(),
        natural_key_count=digest.row_count,
        natural_key_hash=digest.key_hash,
        identity=identity,
    )


def _candidate_from_header(
    category: str,
    sheet_name: str,
    header_row_no: int,
    row: Sequence[object | None],
) -> SourceCandidate | None:
    required = _required_headers(category)
    keys, collisions = _normalized_header_keys(row)
    if not set(required).issubset(keys):
        return None
    if category == "iqvia_nsa" and not _has_nsa_period_metrics(keys):
        return None
    if collisions:
        raise SourceFingerprintError(f"normalized-header collision: {sorted(collisions)}")
    forbidden = {
        normalize_header(header) or ""
        for header in FORBIDDEN_HEADERS.get(category, ())
    }
    present_forbidden = sorted(forbidden.intersection(keys))
    if present_forbidden:
        raise SourceFingerprintError(
            f"forbidden columns for {category}: {present_forbidden}"
        )
    indexes = dict(keys)
    return SourceCandidate(category, sheet_name, header_row_no, frozenset(keys), indexes)


def _required_headers(category: str) -> tuple[str, ...]:
    match category:
        case "iqvia_csd_channel":
            return tuple(normalize_header(header) or "" for header in CSD_HEADERS)
        case "iqvia_csd_keyword":
            return tuple(normalize_header(header) or "" for header in KEYWORD_HEADERS)
        case "iqvia_nsa":
            return tuple(normalize_header(header) or "" for header in NSA_REQUIRED_HEADERS)
        case _:
            raise SourceFingerprintError(f"unsupported source fingerprint category {category!r}")


def _normalized_header_keys(row: Sequence[object | None]) -> tuple[dict[str, int], list[str]]:
    keys: dict[str, int] = {}
    collisions: list[str] = []
    for index, value in enumerate(row):
        key = normalize_header(value)
        if key is None:
            continue
        if key in keys:
            collisions.append(key)
            continue
        keys[key] = index
    return keys, collisions


def _has_nsa_period_metrics(keys: dict[str, int]) -> bool:
    if normalize_header("DATA PERIOD") in keys:
        return all((normalize_header(metric) or "") in keys for metric in NSA_METRIC_HEADERS)
    periods: dict[str, set[str]] = {}
    for key in keys:
        match = re.fullmatch(r"(\d{1,2})/(\d{4})_(.+)", key)
        if match is None:
            continue
        period = f"{int(match.group(2)):04d}-{int(match.group(1)):02d}"
        periods.setdefault(period, set()).add(match.group(3))
    required = {normalize_header(metric) or "" for metric in NSA_METRIC_HEADERS}
    return any(required.issubset(metrics) for metrics in periods.values())


def _natural_key_digest(
    path: Path,
    candidates: Sequence[SourceCandidate],
) -> NaturalKeyDigest:
    workbook = open_workbook_by_content(path)
    try:
        with TemporaryDirectory(prefix="source-fingerprint-") as temp_root:
            connection = sqlite3.connect(Path(temp_root) / "natural_keys.sqlite")
            try:
                connection.execute("CREATE TABLE natural_keys (key_json TEXT PRIMARY KEY)")
                periods: set[str] = set()
                row_count = 0
                for candidate in candidates:
                    sheet = workbook[candidate.sheet_name]
                    rows = sheet.iter_rows(
                        min_row=candidate.header_row_no + 1,
                        values_only=True,
                    )
                    for source_row_no, values in enumerate(
                        rows,
                        start=candidate.header_row_no + 1,
                    ):
                        if not any(normalize_cell(value) for value in values):
                            continue
                        keys = tuple(_row_keys(candidate, values))
                        if not keys:
                            continue
                        for key in keys:
                            if any(not part for part in key):
                                raise SourceFingerprintError(
                                    f"null natural key for {candidate.category}"
                                )
                            identity_key = (
                                (*key, str(source_row_no))
                                if candidate.category == "iqvia_csd_keyword"
                                else key
                            )
                            key_json = json.dumps(
                                identity_key,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            )
                            try:
                                connection.execute(
                                    "INSERT INTO natural_keys (key_json) VALUES (?)",
                                    (key_json,),
                                )
                            except sqlite3.IntegrityError as exc:
                                raise SourceFingerprintError(
                                    f"duplicate natural key for {candidate.category}: {key}"
                                ) from exc
                            periods.add(key[0])
                            row_count += 1
                return NaturalKeyDigest(
                    periods=frozenset(periods),
                    row_count=row_count,
                    key_hash=_natural_key_hash(connection),
                )
            finally:
                connection.close()
    finally:
        workbook.close()


def _natural_key_hash(connection: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    for (key_json,) in connection.execute("SELECT key_json FROM natural_keys ORDER BY key_json"):
        digest.update(key_json.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _row_keys(candidate: SourceCandidate, values: Sequence[object | None]) -> Iterable[tuple[str, ...]]:
    match candidate.category:
        case "iqvia_csd_channel":
            if _cell_text(candidate, values, "Region").casefold() != "total":
                return ()
            return (
                _key_from_headers(
                    candidate,
                    values,
                    ("Related date", "Market", "JW Channel", "Master product", "Representing Company"),
                ),
            )
        case "iqvia_csd_keyword":
            return (
                _key_from_headers(
                    candidate,
                    values,
                    (
                        "Related date",
                        "VISIT LOCATION",
                        "SPECIALTY NAME",
                        "REP# CO",
                        "PRODUCT NAME",
                        "THERAPEUTIC CLASS",
                        "KEYWORDS",
                    ),
                ),
            )
        case "iqvia_nsa":
            return _nsa_row_keys(candidate, values)
        case _:
            raise SourceFingerprintError(f"unsupported source fingerprint category {candidate.category!r}")


def _nsa_row_keys(candidate: SourceCandidate, values: Sequence[object | None]) -> tuple[tuple[str, ...], ...]:
    base_key = (
        normalize_cell(_value(values, candidate.indexes[normalize_header("AUDIT CODE") or ""])),
        normalize_cell(_value(values, candidate.indexes[normalize_header("MFR CODE") or ""])),
        normalize_cell(_value(values, candidate.indexes[normalize_header("PRODUCT NAME") or ""])),
        normalize_cell(_value(values, candidate.indexes[normalize_header("PACK DESC") or ""])),
    )
    period_key = normalize_header("DATA PERIOD") or ""
    if period_key in candidate.indexes:
        return ((parse_quarter_period(_value(values, candidate.indexes[period_key])), *base_key),)
    period_metrics = _nsa_period_metric_keys(candidate)
    return tuple(
        (period, *base_key)
        for period, metric_keys in sorted(period_metrics.items())
        if any(
            normalize_cell(_value(values, candidate.indexes[metric_key]))
            for metric_key in metric_keys
        )
    )


def _nsa_period_metric_keys(candidate: SourceCandidate) -> dict[str, tuple[str, ...]]:
    periods: dict[str, list[str]] = {}
    for key in candidate.header_keys:
        match = re.fullmatch(r"(\d{1,2})/(\d{4})_(.+)", key)
        if match is None:
            continue
        month = int(match.group(1))
        if month not in {3, 6, 9, 12}:
            continue
        period = f"{int(match.group(2)):04d}-Q{((month - 1) // 3) + 1}"
        periods.setdefault(period, []).append(key)
    return {period: tuple(metric_keys) for period, metric_keys in periods.items()}


def _key_from_headers(
    candidate: SourceCandidate,
    values: Sequence[object | None],
    headers: tuple[str, ...],
) -> tuple[str, ...]:
    period_header, *identity_headers = headers
    return (
        parse_month_period(_cell_value(candidate, values, period_header)),
        *(
            _cell_text(candidate, values, header)
            for header in identity_headers
        ),
    )


def _cell_text(candidate: SourceCandidate, values: Sequence[object | None], header: str) -> str:
    return normalize_cell(_cell_value(candidate, values, header))


def _cell_value(candidate: SourceCandidate, values: Sequence[object | None], header: str) -> object | None:
    return _value(values, candidate.indexes[normalize_header(header) or ""])


def _value(values: Sequence[object | None], index: int) -> object | None:
    return values[index] if index < len(values) else None


def parse_month_period(value: object) -> str:
    text = normalize_cell(value)
    match = re.search(r"\b(20\d{2})[./-](\d{1,2})(?:[./-]\d{1,2})?\b", text)
    if match is not None:
        return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}"
    match = re.search(r"\b([A-Za-z]+)\.?\s*(\d{2}|\d{4})\b", text)
    if match is None:
        raise SourceFingerprintError(f"unparseable source period: {value!r}")
    month = MONTHS.get(match.group(1).casefold())
    if month is None:
        raise SourceFingerprintError(f"unparseable source month: {value!r}")
    year_raw = int(match.group(2))
    year = 2000 + year_raw if year_raw < 100 else year_raw
    return f"{year:04d}-{month:02d}"


def parse_quarter_period(value: object) -> str:
    month_period = parse_month_period(value)
    year, month = month_period.split("-")
    month_value = int(month)
    if month_value not in {3, 6, 9, 12}:
        raise SourceFingerprintError(f"NSA period is not quarter-ending: {value!r}")
    return f"{year}-Q{((month_value - 1) // 3) + 1}"


def normalize_loader_header(value: object) -> str | None:
    return normalize_header(value) or normalize_source_header(value)
