"""Local CSV preprocessor: decode -> parse -> (columns, rows) for the file-SQL path.

CSV is handled entirely by the bridge's local path (like xlsx/xlsm): it never
touches the DB extension whitelist or the external preprocessor. The only net-new
work over the xlsx path is turning bytes into (columns, rows); header detection,
column de-duplication, the SQL logical table, the scoped-query engine, security
(SELECT-only), and cleanup are all reused (file_sql + xlsx header helpers).

Header/type rules are intentionally shared with xlsx (`_first_header`,
`_dedupe_headers`); values are emitted as text (str | None) so SQLite dynamic
typing behaves exactly as it does for xlsx SQL sheets.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .xlsx_preprocessor import _dedupe_headers, _first_header

# Encoding fallback order. UTF-8-SIG first so a BOM is consumed rather than
# surfacing as a stray leading character; then strict UTF-8; then the two Korean
# code pages that dominate domestic exports. No heuristic guessing beyond this —
# an undecodable file is rejected explicitly rather than mojibake-passed.
CSV_ENCODINGS: Final = ("utf-8-sig", "utf-8", "cp949", "euc-kr")
# Delimiters the sniffer is allowed to pick; anything else falls back to comma.
CSV_DELIMITERS: Final = ",\t;|"
_SNIFF_BYTES: Final = 65536


class CsvPreprocessError(RuntimeError):
    """Raised when a CSV file cannot be decoded or has no usable header."""


@dataclass(frozen=True, slots=True)
class CsvSqlTable:
    """Parsed CSV ready for provision_session_table(columns, rows())."""

    columns: tuple[str, ...]
    data_rows: tuple[tuple[str | None, ...], ...]
    encoding: str
    delimiter: str

    @property
    def row_count(self) -> int:
        return len(self.data_rows)

    @property
    def column_count(self) -> int:
        return len(self.columns)

    def rows(self) -> Iterator[tuple[str | None, ...]]:
        yield from self.data_rows


def decode_csv_bytes(raw: bytes) -> tuple[str, str]:
    """Return (text, encoding) using the fixed fallback order, else reject."""
    for encoding in CSV_ENCODINGS:
        try:
            return raw.decode(encoding), encoding
        except (UnicodeDecodeError, LookupError):
            continue
    raise CsvPreprocessError(
        "CSV 인코딩을 인식할 수 없습니다. UTF-8 또는 CP949(EUC-KR)로 저장해 다시 시도해 주세요."
    )


def _sniff_delimiter(sample: str) -> str:
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=CSV_DELIMITERS)
    except csv.Error:
        return ","
    delimiter = dialect.delimiter
    return delimiter if delimiter in CSV_DELIMITERS else ","


def _iter_raw_rows(text: str, delimiter: str) -> Iterator[list[str]]:
    # csv.reader over an in-memory line iterator keeps memory bounded to the
    # reader's row buffer rather than materializing the whole file as a matrix.
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    for row in reader:
        yield [cell for cell in row]


def parse_csv_table(path: Path) -> CsvSqlTable:
    """Parse a CSV file into a single logical SQL table (columns + text rows)."""
    raw = path.read_bytes()
    text, encoding = decode_csv_bytes(raw)
    if not text.strip():
        raise CsvPreprocessError("CSV 파일이 비어 있습니다.")

    delimiter = _sniff_delimiter(text[:_SNIFF_BYTES])

    all_rows = list(_iter_raw_rows(text, delimiter))
    header = _first_header(all_rows)
    if header is None:
        raise CsvPreprocessError("CSV에서 사용할 수 있는 헤더 행을 찾지 못했습니다.")
    header_index, columns_list = header
    columns = tuple(_dedupe_headers(columns_list))
    width = len(columns)

    data_rows: list[tuple[str | None, ...]] = []
    for row in all_rows[header_index + 1 :]:
        if not any(cell.strip() for cell in row):
            continue  # skip fully blank rows, matching the xlsx sheet reader
        normalized: list[str | None] = []
        for column_index in range(width):
            value = row[column_index] if column_index < len(row) else None
            normalized.append(value if value not in ("", None) else None)
        data_rows.append(tuple(normalized))

    return CsvSqlTable(
        columns=columns,
        data_rows=tuple(data_rows),
        encoding=encoding,
        delimiter=delimiter,
    )
