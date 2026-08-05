from __future__ import annotations

import hashlib
import json
from pathlib import Path

import openpyxl
import pytest
from fastapi.testclient import TestClient

from pipeline.scripts.ingest_hook.app import IngestService, create_app
from pipeline.scripts.ingest_hook.workbook_source_validation import (
    SourceValidationError,
    detect_workbook_source,
)


def _save(path: Path, rows: list[list[object]], *, title: str = "Data", header_row: int = 1) -> Path:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = title
    for _ in range(header_row - 1):
        sheet.append([])
    for row in rows:
        sheet.append(row)
    workbook.save(path)
    workbook.close()
    return path


def _nsa(path: Path, reverse: bool = False) -> Path:
    headers = ["DATA PERIOD", "AUDIT CODE", "MFR CODE", "PRODUCT NAME", "PACK DESC", "Values LC", "Units", "Counting Units", "Dosage Units", "Price"]
    values = ["2026-03", "A", "M", "Brand", "Pack", 1, 1, 1, 1, 1]
    if reverse:
        headers.reverse()
        values.reverse()
    return _save(path, [headers, values])


def _csd(path: Path, reverse: bool = False) -> Path:
    from pipeline.scripts.etl.brand_activity.csd_core import EXPECTED_HEADERS

    headers = list(EXPECTED_HEADERS)
    values = ["Mar. 26", "Market", "TOTAL", "TOTAL", "Brand", "Maker", "JW", 1]
    if reverse:
        headers.reverse()
        values.reverse()
    return _save(path, [headers, values], header_row=7)


def _keyword(path: Path, reverse: bool = False) -> Path:
    from pipeline.scripts.etl.brand_activity.ingest_keyword import KEYWORD_HEADERS

    headers = list(KEYWORD_HEADERS)
    values = ["Mar. 26", "Seoul", "IM", "JW", "Brand", "A10", "keyword", "high", "1", "up", "N", "N", "N", "N", "N", "", ""]
    if reverse:
        headers.reverse()
        values.reverse()
    return _save(path, [headers, values])


def _ubist(path: Path, reverse: bool = False) -> Path:
    headers1 = [None, "처방조제액(원)"]
    headers2 = ["제품", "2026년 3월"]
    values = ["Brand", 1]
    if reverse:
        headers1.reverse()
        headers2.reverse()
        values.reverse()
    return _save(path, [headers1, headers2, values])


@pytest.mark.parametrize(
    ("category", "builder"),
    [("ubist", _ubist), ("iqvia_nsa", _nsa), ("iqvia_csd_channel", _csd), ("iqvia_csd_keyword", _keyword)],
)
@pytest.mark.parametrize("reverse", [False, True])
def test_classifier_uses_headers_and_ignores_column_order(tmp_path, category, builder, reverse):
    path = builder(tmp_path / "arbitrary-name.xlsx", reverse)

    assert detect_workbook_source(path) == category


@pytest.mark.parametrize(
    ("category", "builder"),
    [("ubist", _ubist), ("iqvia_nsa", _nsa), ("iqvia_csd_channel", _csd), ("iqvia_csd_keyword", _keyword)],
)
def test_reversed_headers_pass_canonical_loader(tmp_path, category, builder):
    path = builder(tmp_path / "content-only.xlsx", True)

    if category == "ubist":
        from pipeline.etl.io.ubist_loader import iter_xlsx_rows

        assert len(list(iter_xlsx_rows(path))) == 1
    else:
        from pipeline.scripts.ingest_hook.workbook_contracts import summarize

        summary = summarize(category, path, "2026-03")
        assert summary.rows == 1
        expected_period = "2026-Q1" if category == "iqvia_nsa" else "2026-03"
        assert summary.periods == frozenset({expected_period})


def test_classifier_rejects_unrecognized_workbook(tmp_path):
    path = _save(tmp_path / "unknown.xlsx", [["foo", "bar"], [1, 2]])

    with pytest.raises(SourceValidationError, match="unrecognized"):
        detect_workbook_source(path)


def test_classifier_rejects_malformed_xlsx_as_source_validation_error(tmp_path):
    path = tmp_path / "broken.xlsx"
    path.write_bytes(b"not-an-xlsx-archive")

    with pytest.raises(SourceValidationError, match="invalid XLSX structure"):
        detect_workbook_source(path)


def test_classifier_reads_only_xlsx_xml_without_openpyxl_workbook_materialization(
    monkeypatch, tmp_path
):
    import openpyxl

    path = _nsa(tmp_path / "large-source-shape.xlsx")
    monkeypatch.setattr(
        openpyxl,
        "load_workbook",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("pre-queue classifier must not materialize the workbook")
        ),
    )

    assert detect_workbook_source(path) == "iqvia_nsa"


def test_webhook_rejects_selected_category_mismatch_before_ledger_or_job(
    tmp_path, sqlite_ledger, fake_transport
):
    workbook = _nsa(tmp_path / "misleading-keyword-name.xlsx")
    payload = {
        "contract_version": "v2",
        "epoch": "2026-03",
        "category": "iqvia_csd_keyword",
        "complete": True,
        "files": [{"path": workbook.name, "sha256": hashlib.sha256(workbook.read_bytes()).hexdigest()}],
    }
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    client = TestClient(create_app(IngestService(sqlite_ledger, tmp_path, transport=fake_transport)))

    response = client.post("/ingest/webhook", json={"manifest_path": manifest.name})

    assert response.status_code == 422
    assert response.json()["detail"] == {
        "code": "source_category_mismatch",
        "selected_category": "iqvia_csd_keyword",
        "detected_category": "iqvia_nsa",
        "message": "선택한 소스와 파일 내용이 일치하지 않습니다.",
    }
    assert sqlite_ledger.queued_categories() == []
    assert fake_transport.submitted == []


def test_nsa_workbook_summary_streams_rows_without_materializing(monkeypatch, tmp_path):
    from pipeline.etl.io import iqvia_loader
    from pipeline.scripts.ingest_hook.workbook_contracts import summarize

    class StreamingRows:
        def __init__(self) -> None:
            self._rows = iter(
                (
                    {"period_yyyy": 2025, "period_quarter": 4},
                    {"period_yyyy": 2026, "period_quarter": 1},
                )
            )

        def __iter__(self):
            return self

        def __next__(self):
            return next(self._rows)

        def __length_hint__(self) -> int:
            raise AssertionError("NSA summary must not materialize the workbook iterator")

    monkeypatch.setattr(iqvia_loader, "iter_nsa_xlsx", lambda _path: StreamingRows())

    summary = summarize("iqvia_nsa", tmp_path / "unused.xlsx", "2026-Q1")

    assert summary.rows == 2
    assert summary.periods == frozenset({"2025-Q4", "2026-Q1"})
