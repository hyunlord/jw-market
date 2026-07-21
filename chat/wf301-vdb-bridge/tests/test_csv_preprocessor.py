from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi import UploadFile

import src.csv_preprocessor as csv_pre
import src.main as main
from src.csv_preprocessor import CsvPreprocessError, parse_csv_table
from src.file_sql.config import FileSqlConfig
from src.file_sql.service import FileSqlService
from src.models import TempDocument
from src.upload_adapter import (
    SavedTempDocument,
    requires_external_preprocessor,
    validate_extensions,
)


def _write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


def test_decode_fallback_prefers_utf8_sig_then_utf8_then_cp949() -> None:
    text, encoding = csv_pre.decode_csv_bytes("품목,값\n리바로,1\n".encode("utf-8-sig"))
    assert encoding == "utf-8-sig"
    assert text.startswith("품목")  # BOM consumed, not a stray leading char

    # A plain UTF-8 file without a BOM decodes under utf-8-sig too (BOM optional),
    # so either label is correct — the text is identical.
    _, encoding = csv_pre.decode_csv_bytes("a,b\n1,2\n".encode("utf-8"))
    assert encoding in {"utf-8", "utf-8-sig"}

    _, encoding = csv_pre.decode_csv_bytes("품목,값\n리바로,1\n".encode("cp949"))
    assert encoding in {"cp949", "euc-kr"}


def test_decode_rejects_undecodable_bytes_instead_of_guessing() -> None:
    with pytest.raises(CsvPreprocessError):
        csv_pre.decode_csv_bytes(b"\xff\xff\xfe\xff")


def test_parse_csv_detects_comma_tab_and_semicolon(tmp_path: Path) -> None:
    for name, delimiter in (("c", ","), ("t", "\t"), ("s", ";")):
        body = "".join(
            f"{delimiter.join(row)}\n"
            for row in (["품목", "매출"], ["리바로", "100"], ["리피토", "200"], ["크레스토", "300"], ["a", "4"])
        )
        table = parse_csv_table(_write(tmp_path / f"{name}.csv", body.encode("utf-8")))
        assert table.columns == ("품목", "매출"), delimiter
        assert table.row_count == 4


def test_parse_csv_dedupes_duplicate_headers(tmp_path: Path) -> None:
    table = parse_csv_table(_write(tmp_path / "d.csv", "name,name,city\n1,2,seoul\n".encode("utf-8")))
    assert table.columns == ("name", "name_2", "city")


def test_parse_csv_skips_blank_rows_and_pads_short_rows(tmp_path: Path) -> None:
    table = parse_csv_table(_write(tmp_path / "p.csv", "a,b,c\n1,2\n\n4,5,6\n".encode("utf-8")))
    assert table.columns == ("a", "b", "c")
    assert list(table.rows()) == [("1", "2", None), ("4", "5", "6")]


def test_parse_csv_rejects_empty_file(tmp_path: Path) -> None:
    with pytest.raises(CsvPreprocessError):
        parse_csv_table(_write(tmp_path / "e.csv", b"   \n"))


def test_csv_round_trip_through_file_sql_engine(tmp_path: Path) -> None:
    """csv upload -> parse -> SQL logical table -> scoped query (the §2-4 chain)."""
    csv_path = _write(
        tmp_path / "sales.csv",
        "품목,매출\n리바로,100\n리피토,200\n".encode("cp949"),  # domestic CP949 file
    )
    table = parse_csv_table(csv_path)
    assert table.columns == ("품목", "매출")
    assert table.encoding in {"cp949", "euc-kr"}

    service = FileSqlService(FileSqlConfig(enabled=True, root_dir=tmp_path / "fsql"))
    service.provision_session_table("sess-1", "sales", table.columns, table.rows())

    counted = service.run_scoped_query("sess-1", "sales", "SELECT COUNT(*) FROM data")
    assert counted.rows[0][0] == 2

    # Source columns are queried via the positional query columns (c1, c2, ...),
    # with the source->query mapping exposed for the LLM — identical to xlsx.
    schema = service.describe_schema_for_llm("sess-1", "sales")
    assert schema.source_columns == ("품목", "매출")
    assert schema.query_columns == ("c1", "c2")
    filtered = service.run_scoped_query("sess-1", "sales", "SELECT c1 FROM data WHERE c2 = '200'")
    assert filtered.rows[0][0] == "리피토"


def test_sql_decision_builds_single_table_for_csv(tmp_path: Path) -> None:
    csv_path = _write(tmp_path / "m.csv", "a,b\n1,2\n3,4\n".encode("utf-8"))
    doc = TempDocument(temp_document_id=7, file_name="m.csv", file_path=str(csv_path))

    decision = main._sql_decision_for_temp_doc(doc)

    assert decision is not None
    assert decision.route == "sql"
    assert len(decision.selected_sheets) == 1
    assert decision.selected_sheets[0].sheet_name == "m.csv"
    assert decision.selected_sheets[0].column_count == 2
    # the shared provision loader returns csv rows via the same interface as xlsx
    data = main._load_sql_sheet_rows(csv_path, decision.selected_sheets[0])
    assert data.columns == ("a", "b")
    assert list(data.rows()) == [("1", "2"), ("3", "4")]


def test_sql_decision_rejects_unreadable_csv_without_raising(tmp_path: Path) -> None:
    bad = _write(tmp_path / "bad.csv", b"\xff\xff\xfe\xff")
    doc = TempDocument(temp_document_id=8, file_name="bad.csv", file_path=str(bad))
    assert main._sql_decision_for_temp_doc(doc) is None  # rejected, not a crash


def test_csv_is_a_local_extension_bypassing_db_and_external() -> None:
    # local -> never delegated to the external preprocessor
    assert requires_external_preprocessor(
        SavedTempDocument(temp_document_id=1, file_name="x.csv", file_path="/tmp/x.csv")
    ) is False
    assert requires_external_preprocessor(
        SavedTempDocument(temp_document_id=2, file_name="x.pdf", file_path="/tmp/x.pdf")
    ) is True
    # local -> accepted even when the DB whitelist does not list csv
    allowed = frozenset({"pdf"})
    csv_upload = UploadFile(file=io.BytesIO(b"a,b\n1,2\n"), filename="x.csv")
    doc_upload = UploadFile(file=io.BytesIO(b"x"), filename="x.doc")
    assert validate_extensions([csv_upload], allowed) == []
    assert validate_extensions([doc_upload], allowed) == ["허용되지 않는 파일 확장자입니다: x.doc"]
