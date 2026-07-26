"""P2' source workbook fingerprinting is based on internal structure only."""
from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.scripts.ingest_hook import workbook_contracts
from pipeline.scripts.ingest_hook.source_fingerprint import SourceFingerprintError, fingerprint_source
from pipeline.scripts.ingest_hook.workbook_contracts import classify, summarize
from pipeline.scripts.etl.brand_activity.raw_extract import read_csd_source_rows


def _save(path: Path, rows_by_sheet: dict[str, list[list[object | None]]]) -> Path:
    import openpyxl

    workbook = openpyxl.Workbook()
    first = True
    for sheet_name, rows in rows_by_sheet.items():
        sheet = workbook.active if first else workbook.create_sheet()
        first = False
        sheet.title = sheet_name
        for row in rows:
            sheet.append(row)
    workbook.save(path)
    return path


def _csd_rows(
    *,
    headers: list[object | None] | None = None,
    data: list[object | None] | None = None,
) -> list[list[object | None]]:
    header = headers or [
        "Related date",
        "Market",
        "JW Channel",
        "Region",
        "Master product",
        "Manufacturer",
        "Representing Company",
        "Product Details",
    ]
    row = data or ["Jul 2026", "Diabetes", "TOTAL", "TOTAL", "Drug A", "Maker", "JW", 10]
    return [[], [], [], [], [], [], header, row]


def _keyword_rows(
    *,
    headers: list[object | None] | None = None,
    data: list[object | None] | None = None,
) -> list[list[object | None]]:
    header = headers or [
        "Related date",
        "VISIT LOCATION",
        "SPECIALTY NAME",
        "REP# CO",
        "PRODUCT NAME",
        "THERAPEUTIC CLASS",
        "KEYWORDS",
        "INTEREST",
        "Prescription frequency",
        "Prescription evolution",
        "Abstract and clinical literature / data",
        "Patient educational literature",
        "Promotional product literature",
        "SAMPLES LEFT",
        "OTHER MATERIALS LEFT",
        "WHAT OTHER MATERIALS",
        "OTHER COMMENTS",
    ]
    row = data or [
        "Jul 2026",
        "Clinic",
        "Endocrinology",
        "JW",
        "Drug A",
        "Diabetes",
        "keyword",
        "high",
        "often",
        "up",
        "yes",
        "yes",
        "yes",
        "no",
        "no",
        "",
        "",
    ]
    return [header, row]


def _nsa_rows(headers: list[object | None] | None = None) -> list[list[object | None]]:
    header = headers or [
        "AUDIT CODE",
        "MFR CODE",
        "PRODUCT NAME",
        "PACK DESC",
        "03/2026_Values LC",
        "03/2026_Units",
        "03/2026_Counting Units",
        "03/2026_Dosage Units",
        "03/2026_Price",
    ]
    return [header, ["A1", "M1", "Drug A", "10mg", 1, 2, 3, 4, 5]]


def test_fingerprint_accepts_equivalent_headers_and_ignores_sheet_names(tmp_path: Path) -> None:
    # Given two CSD workbooks with unrelated sheet names, shuffled columns, extra
    # columns, case/space changes, and a BOM on one header.
    first = _save(tmp_path / "first.bin", {"Random": _csd_rows()})
    second_headers = [
        " ignored extra ",
        " PRODUCT DETAILS ",
        "representing   company",
        "\ufeffrelated date",
        "manufacturer",
        "market",
        "REGION",
        "master product",
        "jw channel",
    ]
    second_row = ["extra", 10, "JW", "Jul 2026", "Maker", "Diabetes", "TOTAL", "Drug A", "TOTAL"]
    second = _save(
        tmp_path / "second.data",
        {"Definitely Not Market": _csd_rows(headers=second_headers, data=second_row)},
    )

    # When both files are fingerprinted by internal workbook structure.
    first_fp = fingerprint_source(first, "iqvia_csd_channel")
    second_fp = fingerprint_source(second, "iqvia_csd_channel")

    # Then the source identity is independent of file name, extension, sheet
    # name, and column order.
    assert first_fp.identity == second_fp.identity
    assert first_fp.periods == frozenset({"2026-07"})
    assert summarize("iqvia_csd_channel", second, "2026-07").rows == 1


def test_csd_fingerprint_and_loader_aggregate_all_content_matching_sheets(tmp_path: Path) -> None:
    first_rows = _csd_rows()
    second_rows = _csd_rows(
        data=["Aug 2026", "Cardiology", "TOTAL", "TOTAL", "Drug B", "Maker B", "JW", 20]
    )
    path = _save(
        tmp_path / "arbitrary.payload",
        {
            "Renamed One": first_rows,
            "Renamed Two": second_rows,
            "Cover": [["not", "source", "data"]],
        },
    )

    fingerprint = fingerprint_source(path, "iqvia_csd_channel")
    rows = read_csd_source_rows(path, "a" * 64)

    assert classify(path, "2026-08") == "iqvia_csd_channel"
    assert fingerprint.natural_key_count == 2
    assert fingerprint.periods == frozenset({"2026-07", "2026-08"})
    assert [(row.period_ym, row.market, row.product_details) for row in rows] == [
        ("2026-07", "Diabetes", 10),
        ("2026-08", "Cardiology", 20),
    ]
    assert all(row.selected_for_stage for row in rows)
    assert {row.source_period_ym for row in rows} == {"2026-07", "2026-08"}


def test_keyword_sheet_name_does_not_matter(tmp_path: Path) -> None:
    # Given a Keyword workbook whose sheet is not named Keywords.
    path = _save(tmp_path / "upload.xlsx", {"User Renamed This": _keyword_rows()})

    # When the workbook contract runs.
    summary = summarize("iqvia_csd_keyword", path, "2026-07")

    # Then content-based discovery finds the row anyway.
    assert summary.rows == 1
    assert summary.periods == frozenset({"2026-07"})


@pytest.mark.parametrize(
    ("rows_by_sheet", "message"),
    [
        ({"NoMatch": [["unrelated"], ["value"]]}, "0 matching"),
        ({"First": _keyword_rows(), "Second": _keyword_rows()}, "multiple matching"),
        (
            {
                "Only": _keyword_rows(
                    data=[
                        "Jul 2026",
                        "Clinic",
                        "Endocrinology",
                        "JW",
                        "",
                        "Diabetes",
                        "keyword",
                        "high",
                        "often",
                        "up",
                        "yes",
                        "yes",
                        "yes",
                        "no",
                        "no",
                        "",
                        "",
                    ]
                )
            },
            "null natural key",
        ),
        (
            {"Only": _keyword_rows(headers=["Related date", "\ufeffrelated date", *_keyword_rows()[0][1:]])},
            "normalized-header collision",
        ),
    ],
)
def test_fingerprint_rejects_ambiguous_or_invalid_keyword_sources(
    tmp_path: Path,
    rows_by_sheet: dict[str, list[list[object | None]]],
    message: str,
) -> None:
    # Given a structurally invalid candidate workbook.
    path = _save(tmp_path / "candidate.xlsx", rows_by_sheet)

    # When/Then internal fingerprinting fails closed.
    with pytest.raises(SourceFingerprintError, match=message):
        fingerprint_source(path, "iqvia_csd_keyword")


def test_keyword_duplicate_events_are_distinct_source_rows(tmp_path: Path) -> None:
    path = _save(
        tmp_path / "repeated-events.xlsx",
        {"Anything": _keyword_rows() + _keyword_rows()[1:]},
    )

    fingerprint = fingerprint_source(path, "iqvia_csd_keyword")

    assert fingerprint.natural_key_count == 2
    assert fingerprint.periods == frozenset({"2026-07"})


def test_nsa_discovery_skips_unrelated_sheets_without_using_sheet_name(tmp_path: Path) -> None:
    # Given an NSA workbook where the parseable sheet has a non-canonical name
    # and an unrelated first sheet would previously poison iteration.
    path = _save(tmp_path / "nsa.payload", {"Cover": [["not", "nsa"]], "Anything": _nsa_rows()})

    # When the loader-backed workbook contract runs.
    summary = summarize("iqvia_nsa", path, "2026-Q1")

    # Then the NSA sheet is found by header/content only.
    assert summary.rows == 1
    assert summary.periods == frozenset({"2026-Q1"})
    assert classify(path, "2026-Q1") == "iqvia_nsa"


def test_forbidden_cross_category_headers_fail_closed(tmp_path: Path) -> None:
    headers = [*_csd_rows()[6], "KEYWORDS"]
    data = [*_csd_rows()[7], "must-not-be-here"]
    path = _save(
        tmp_path / "cross-category.xlsx",
        {"Any": _csd_rows(headers=headers, data=data)},
    )

    with pytest.raises(SourceFingerprintError, match="forbidden columns"):
        fingerprint_source(path, "iqvia_csd_channel")


def test_long_form_nsa_period_column_is_name_mapped(tmp_path: Path) -> None:
    headers = [
        "PACK DESC",
        "DATA PERIOD",
        "PRODUCT NAME",
        "MFR CODE",
        "AUDIT CODE",
        "Values LC",
        "Units",
        "Counting Units",
        "Dosage Units",
        "Price",
    ]
    row = ["10mg", "Mar 2026", "Drug A", "M1", "A1", 1, 2, 3, 4, 5]
    path = _save(tmp_path / "nsa-long.payload", {"Anything": [headers, row]})

    fingerprint = fingerprint_source(path, "iqvia_nsa")

    assert fingerprint.periods == frozenset({"2026-Q1"})


def test_fingerprint_uses_bounded_identity_without_returning_all_natural_keys(tmp_path: Path) -> None:
    # Given a workbook large enough that returning every natural key would make
    # the public fingerprint shape grow with source row count.
    header = _keyword_rows()[0]
    rows = [header]
    for index in range(1_200):
        row = _keyword_rows()[1].copy()
        row[1] = f"Clinic {index}"
        row[6] = f"keyword {index}"
        rows.append(row)
    path = _save(tmp_path / "large-keyword.xlsx", {"Keyword": rows})

    # When the fingerprint is calculated.
    fingerprint = fingerprint_source(path, "iqvia_csd_keyword")

    # Then the contract exposes bounded count/hash identity fields instead of
    # materializing the full natural key set on the result.
    assert fingerprint.natural_key_count == 1_200
    assert fingerprint.natural_key_hash
    assert fingerprint.natural_keys == frozenset()
    assert fingerprint.periods == frozenset({"2026-07"})


def test_summary_uses_fingerprint_count_without_reloading_csd_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a CSD workbook whose fingerprint can validate the TOTAL rows.
    path = _save(tmp_path / "csd.xlsx", {"Market": _csd_rows()})

    def fail_iter_market_rows(*_args: object, **_kwargs: object) -> list[object]:
        raise AssertionError("summary must not materialize CSD rows after fingerprinting")

    import pipeline.scripts.etl.brand_activity.csd_core as csd_core

    monkeypatch.setattr(csd_core, "iter_market_rows", fail_iter_market_rows)

    # When the workbook summary is calculated.
    summary = summarize("iqvia_csd_channel", path, "2026-07")

    # Then it uses the bounded fingerprint count path.
    assert summary.rows == 1
    assert summary.periods == frozenset({"2026-07"})


def test_summary_uses_fingerprint_count_without_reloading_keyword_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a Keyword workbook whose fingerprint can validate event rows.
    path = _save(tmp_path / "keyword.xlsx", {"Keyword": _keyword_rows()})

    def fail_read_keyword_events(_path: Path) -> list[object]:
        raise AssertionError("summary must not materialize keyword rows after fingerprinting")

    import pipeline.scripts.etl.brand_activity.ingest_keyword as ingest_keyword

    monkeypatch.setattr(ingest_keyword, "read_keyword_events", fail_read_keyword_events)

    # When the workbook summary is calculated.
    summary = summarize("iqvia_csd_keyword", path, "2026-07")

    # Then it uses the bounded fingerprint count path.
    assert summary.rows == 1
    assert summary.periods == frozenset({"2026-07"})


def test_nsa_summary_does_not_list_loader_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Given an NSA workbook summary path where global list() is guarded.
    path = _save(tmp_path / "nsa.xlsx", {"NSA": _nsa_rows()})

    def fail_list(_value: object) -> list[object]:
        raise AssertionError("NSA summary must not list(iter_nsa_xlsx(...))")

    monkeypatch.setattr(workbook_contracts, "list", fail_list, raising=False)

    # When the workbook summary is calculated.
    summary = summarize("iqvia_nsa", path, "2026-Q1")

    # Then the bounded fingerprint count path is enough for the summary.
    assert summary.rows == 1
    assert summary.periods == frozenset({"2026-Q1"})


def test_nsa_fingerprint_counts_each_wide_period_metric_row(tmp_path: Path) -> None:
    # Given one NSA source row with two populated quarter metric groups.
    headers = [
        "AUDIT CODE",
        "MFR CODE",
        "PRODUCT NAME",
        "PACK DESC",
        "03/2026_Values LC",
        "03/2026_Units",
        "03/2026_Counting Units",
        "03/2026_Dosage Units",
        "03/2026_Price",
        "06/2026_Values LC",
        "06/2026_Units",
        "06/2026_Counting Units",
        "06/2026_Dosage Units",
        "06/2026_Price",
    ]
    row = ["A1", "M1", "Drug A", "10mg", 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    path = _save(tmp_path / "nsa-wide.xlsx", {"NSA": [headers, row]})

    # When the bounded fingerprint and summary are calculated.
    fingerprint = fingerprint_source(path, "iqvia_nsa")
    summary = summarize("iqvia_nsa", path, "2026-Q1")

    # Then the old loader's per-period metric-row contract is preserved.
    assert fingerprint.natural_key_count == 2
    assert fingerprint.periods == frozenset({"2026-Q1", "2026-Q2"})
    assert summary.rows == 2
    assert summary.periods == frozenset({"2026-Q1", "2026-Q2"})
