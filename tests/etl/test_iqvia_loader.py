import json
import logging
import unicodedata
from pathlib import Path

import openpyxl
import pytest

from pipeline.etl.io.iqvia_loader import (
    HeaderContractError,
    canonical_nsa_files,
    deduplicate_nsa_records,
    dry_run,
    iter_record_parquet_records,
    iter_nsa_xlsx,
    long_format_period_record,
    materialize_record_parquet,
)
from pipeline.etl.io.ubist_loader import classify_sheet


CANONICAL_HEADERS = [
    "DATA PERIOD",
    "AUDIT CODE",
    "AUDIT DESC",
    "MFR CODE",
    "MFR NAME",
    "PRODUCT NAME",
    "PACK DESC",
    "Values LC",
    "Units",
    "Counting Units",
    "Dosage Units",
    "Price",
]


def _fullwidth_ascii(text: str) -> str:
    return "".join(
        "\u3000" if character == " " else chr(ord(character) + 0xFEE0)
        if "!" <= character <= "~"
        else character
        for character in text
    )


def _row(*, values_lc: int = 7_152_613) -> list[object]:
    return [
        "2021-06-01 00:00:00",
        "KCPA",
        "Korea Direct Clinic Pharmaceutical Audit",
        "A+K",
        "AUSKOREA",
        "AUSTAREN F",
        "A.IM 90MG 2ML",
        values_lc,
        7537,
        15074,
        7537,
        949,
    ]


def _write_workbook(path: Path, headers: list[str], rows: list[list[object]]) -> Path:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "NSA"
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    workbook.save(path)
    return path


def test_canonical_nsa_files_keeps_workbook_extracts() -> None:
    files = [
        Path("data/IQVIA/NSA/~$KOR_NSA_Jun-25-2026.xlsx"),
        Path("data/IQVIA/NSA/KOR_NSA_Jun-25-2026.xlsx"),
        Path("data/IQVIA/NSA/legacy.csv"),
        Path("data/IQVIA/NSA/readme.txt"),
    ]

    assert canonical_nsa_files(files) == [
        Path("data/IQVIA/NSA/KOR_NSA_Jun-25-2026.xlsx"),
        Path("data/IQVIA/NSA/legacy.csv"),
    ]


def test_long_format_period_record_parses_data_period_rows() -> None:
    raw = {
        "DATA PERIOD": "2021-06-01 00:00:00",
        "AUDIT CODE": "KCPA",
        "AUDIT DESC": "Korea Direct Clinic Pharmaceutical Audit",
        "MFR CODE": "A+K",
        "MFR NAME": "AUSKOREA",
        "PRODUCT NAME": "AUSTAREN F",
        "PACK DESC": "A.IM 90MG 2ML",
        "Values LC": 7152613,
        "Units": 7537,
        "Counting Units": 15074,
        "Dosage Units": 7537,
        "Price": 949,
    }

    record = long_format_period_record(Path("KOR_NSA_Jun-25-2026.xlsx"), "NSA", 2, raw, list(raw))

    assert record is not None
    assert record["period_yyyy"] == 2021
    assert record["period_quarter"] == 2
    assert record["period_label"] == "2021Q2"
    assert record["audit_code"] == "KCPA"


@pytest.mark.parametrize(
    "headers",
    [
        [header.swapcase() for header in CANONICAL_HEADERS],
        [unicodedata.normalize("NFD", header) for header in CANONICAL_HEADERS],
        [f"  {header.replace(' ', '   ')}  " for header in CANONICAL_HEADERS],
        [_fullwidth_ascii(header) for header in CANONICAL_HEADERS],
    ],
)
def test_nsa_header_variants_produce_canonical_record(
    tmp_path: Path, headers: list[str]
) -> None:
    path = _write_workbook(tmp_path / "variant.xlsx", headers, [_row()])

    records = list(iter_nsa_xlsx(path))

    assert len(records) == 1
    assert records[0]["audit_code"] == "KCPA"
    assert records[0]["mfr_code"] == "A+K"
    payload = json.loads(records[0]["payload"])
    assert payload["static"]["PRODUCT NAME"] == "AUSTAREN F"
    assert payload["period_values"]["Values LC"] == 7_152_613


def test_nsa_header_order_does_not_change_record(tmp_path: Path) -> None:
    order = list(reversed(range(len(CANONICAL_HEADERS))))
    headers = [CANONICAL_HEADERS[index] for index in order]
    row = [_row()[index] for index in order]
    path = _write_workbook(tmp_path / "reordered.xlsx", headers, [row])

    [record] = list(iter_nsa_xlsx(path))

    assert record["audit_code"] == "KCPA"
    assert json.loads(record["payload"])["period_values"]["Values LC"] == 7_152_613


def test_nsa_quarter_filter_matches_full_read_subset(tmp_path: Path) -> None:
    q1 = _row(values_lc=100)
    q1[0] = "2026-03-01 00:00:00"
    q2 = _row(values_lc=200)
    q2[0] = "2026-06-01 00:00:00"
    path = _write_workbook(tmp_path / "quarters.xlsx", CANONICAL_HEADERS, [q1, q2])

    full = list(iter_nsa_xlsx(path))
    filtered = list(iter_nsa_xlsx(path, quarters=("2026-Q2",)))

    assert [record["period_label"] for record in full] == ["2026Q1", "2026Q2"]
    assert filtered == [record for record in full if record["period_label"] == "2026Q2"]


def test_nsa_quarter_filter_rejects_invalid_labels(tmp_path: Path) -> None:
    path = _write_workbook(tmp_path / "quarters.xlsx", CANONICAL_HEADERS, [_row()])

    with pytest.raises(ValueError, match="invalid IQVIA quarter"):
        list(iter_nsa_xlsx(path, quarters=("2026-Q5",)))


def test_wide_nsa_quarter_columns_use_same_filter_contract(tmp_path: Path) -> None:
    static_headers = ["AUDIT CODE", "MFR CODE", "PRODUCT NAME", "PACK DESC"]
    metrics = ["Values LC", "Units", "Counting Units", "Dosage Units", "Price"]
    headers = static_headers + [
        f"{month}/{year}_{metric}"
        for month, year in ((3, 2026), (6, 2026))
        for metric in metrics
    ]
    row = ["KCPA", "MFR", "PRODUCT", "10MG"] + [
        value
        for values in ((100, 10, 5, 8, 1), (200, 20, 10, 16, 2))
        for value in values
    ]
    path = _write_workbook(tmp_path / "wide.xlsx", headers, [row])

    records = list(iter_nsa_xlsx(path, quarters=("2026-Q2",)))

    assert len(records) == 1
    assert records[0]["period_label"] == "2026Q2"
    assert json.loads(records[0]["payload"])["period_values"]["Values LC"] == 200


@pytest.mark.parametrize("missing", ["AUDIT CODE", "MFR CODE", "PRODUCT NAME", "PACK DESC", "DATA PERIOD"])
def test_nsa_missing_required_header_fails(tmp_path: Path, missing: str) -> None:
    index = CANONICAL_HEADERS.index(missing)
    headers = CANONICAL_HEADERS[:index] + CANONICAL_HEADERS[index + 1 :]
    row = _row()[:index] + _row()[index + 1 :]
    path = _write_workbook(tmp_path / "missing.xlsx", headers, [row])

    with pytest.raises(HeaderContractError, match=missing):
        list(iter_nsa_xlsx(path))


def test_nsa_dry_run_propagates_header_failure(tmp_path: Path) -> None:
    headers = [header for header in CANONICAL_HEADERS if header != "AUDIT CODE"]
    row = [value for index, value in enumerate(_row()) if CANONICAL_HEADERS[index] != "AUDIT CODE"]
    path = _write_workbook(tmp_path / "missing.xlsx", headers, [row])

    with pytest.raises(HeaderContractError, match="AUDIT CODE"):
        dry_run([path], tmp_path / "dry-run.md")


@pytest.mark.parametrize("missing", ["Values LC", "Units", "Counting Units", "Dosage Units", "Price"])
def test_nsa_missing_metric_header_fails(tmp_path: Path, missing: str) -> None:
    keep = [index for index, header in enumerate(CANONICAL_HEADERS) if header != missing]
    path = _write_workbook(
        tmp_path / "missing-metrics.xlsx",
        [CANONICAL_HEADERS[index] for index in keep],
        [[_row()[index] for index in keep]],
    )

    with pytest.raises(HeaderContractError, match=missing):
        list(iter_nsa_xlsx(path))


def test_nsa_optional_and_unknown_headers_warn_without_blocking(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    headers = [header for header in CANONICAL_HEADERS if header != "MFR NAME"] + ["NEW SOURCE NOTE"]
    row = [value for index, value in enumerate(_row()) if CANONICAL_HEADERS[index] != "MFR NAME"] + ["note"]
    path = _write_workbook(tmp_path / "optional.xlsx", headers, [row])

    with caplog.at_level(logging.WARNING):
        records = list(iter_nsa_xlsx(path))

    assert len(records) == 1
    assert "optional" in caplog.text
    assert "MFR NAME" in caplog.text
    assert "unexpected" in caplog.text
    assert "NEW SOURCE NOTE" in caplog.text


def _record(*, source_file: str, source_row_no: int, values_lc: int) -> dict[str, object]:
    raw = dict(zip(CANONICAL_HEADERS, _row(values_lc=values_lc)))
    record = long_format_period_record(
        Path(source_file), "NSA", source_row_no, raw, list(raw)
    )
    assert record is not None
    return record


def test_nsa_dedup_collapses_exact_rows_and_prefers_later_source() -> None:
    older = _record(source_file="KOR_NSA_May-25-2026.xlsx", source_row_no=2, values_lc=10)
    newer = _record(source_file="KOR_NSA_Jun-25-2026.xlsx", source_row_no=4, values_lc=10)

    records, report = deduplicate_nsa_records([newer, older], "2021Q2")

    assert records == [newer]
    assert report.duplicate_rows_removed == 1
    assert report.conflict_groups == 0


def test_nsa_dedup_preserves_same_key_with_different_metrics() -> None:
    first = _record(source_file="first.xlsx", source_row_no=2, values_lc=10)
    second = _record(source_file="second.xlsx", source_row_no=2, values_lc=11)

    records, report = deduplicate_nsa_records([first, second], "2021Q2")

    assert records == [first, second]
    assert report.duplicate_rows_removed == 0
    assert report.conflict_groups == 1
    assert report.conflict_rows == 2


def test_record_parquet_deduplicates_repeated_file(tmp_path: Path) -> None:
    source = _write_workbook(tmp_path / "same.xlsx", CANONICAL_HEADERS, [_row()])
    output = tmp_path / "records"

    written = materialize_record_parquet([source, source], output, batch_size=1)
    records = list(iter_record_parquet_records(output))

    assert written == {"2021Q2": 1}
    assert len(records) == 1


def test_record_parquet_deduplicates_overlap_across_files(tmp_path: Path) -> None:
    older = _write_workbook(tmp_path / "a.xlsx", CANONICAL_HEADERS, [_row()])
    newer = _write_workbook(tmp_path / "b.xlsx", CANONICAL_HEADERS, [_row()])
    output = tmp_path / "records"

    written = materialize_record_parquet([older, newer], output, batch_size=1)
    [record] = list(iter_record_parquet_records(output))

    assert written == {"2021Q2": 1}
    assert record["source_file"] == "b.xlsx"


def test_record_parquet_preserves_conflicting_overlap(tmp_path: Path) -> None:
    first = _write_workbook(tmp_path / "a.xlsx", CANONICAL_HEADERS, [_row(values_lc=10)])
    second = _write_workbook(tmp_path / "b.xlsx", CANONICAL_HEADERS, [_row(values_lc=11)])
    output = tmp_path / "records"

    written = materialize_record_parquet([first, second], output, batch_size=1)
    records = list(iter_record_parquet_records(output))

    assert written == {"2021Q2": 2}
    values = [
        json.loads(record["payload"])["period_values"]["Values LC"]
        for record in records
    ]
    assert values == [10, 11]


def test_ubist_and_nsa_share_compatibility_header_normalization() -> None:
    mapping = classify_sheet(
        "Sheet1",
        (_fullwidth_ascii("처방조제액(원)"), None),
        ("2026년 5월", _fullwidth_ascii("제품")),
    )

    assert mapping.metric_cols == [(0, "처방조제액(원)", "rx_amt", "2026-05")]
    assert mapping.dim_cols == [(1, _fullwidth_ascii("제품"), "제품")]
