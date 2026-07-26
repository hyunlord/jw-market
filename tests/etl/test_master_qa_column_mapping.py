from __future__ import annotations

import json
from pathlib import Path

import pytest
from openpyxl import Workbook

from pipeline.etl.io.catalog.master.qa import load_qa_records


INGESTED_AT = "2026-07-26 00:00:00"
BASE_HEADERS = ("번호", "질문 유형", "시장명", "질문", "답변", "비고", "추가 정보")
BASE_VALUES = (1, "시장 정의", "제이클", "시장은?", "정의입니다.", "검토 완료", "보존")


def _write_qa_workbook(path: Path, headers: tuple[str, ...], values: tuple[object, ...]) -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Q&A"
    worksheet.append(("제목",))
    worksheet.append(headers)
    worksheet.append(values)
    workbook.save(path)
    workbook.close()


def test_load_qa_records_returns_identical_records_when_columns_are_shuffled(
    tmp_path: Path,
) -> None:
    # Given
    original_path = tmp_path / "original" / "master.xlsx"
    shuffled_path = tmp_path / "shuffled" / "master.xlsx"
    original_path.parent.mkdir()
    shuffled_path.parent.mkdir()
    _write_qa_workbook(original_path, BASE_HEADERS, BASE_VALUES)
    shuffled_order = (4, 6, 2, 0, 5, 3, 1)
    _write_qa_workbook(
        shuffled_path,
        tuple(BASE_HEADERS[index] for index in shuffled_order),
        tuple(BASE_VALUES[index] for index in shuffled_order),
    )

    # When
    original_records, original_stats = load_qa_records(original_path, ingested_at=INGESTED_AT)
    shuffled_records, shuffled_stats = load_qa_records(shuffled_path, ingested_at=INGESTED_AT)

    # Then
    assert shuffled_records == original_records
    assert shuffled_stats == original_stats


def test_load_qa_records_accepts_bom_case_and_outer_space_header_variants(
    tmp_path: Path,
) -> None:
    # Given
    workbook_path = tmp_path / "master.xlsx"
    normalized_headers = (
        "번호",
        "\ufeff QUESTION_TYPE ",
        " MARKET_NAME ",
        " QUESTION_TEXT ",
        " ANSWER_TEXT ",
        " SOURCE_REMARK ",
        "추가 정보",
    )
    shuffled_order = (5, 2, 6, 4, 1, 0, 3)
    _write_qa_workbook(
        workbook_path,
        tuple(normalized_headers[index] for index in shuffled_order),
        tuple(BASE_VALUES[index] for index in shuffled_order),
    )

    # When
    records, _ = load_qa_records(workbook_path, ingested_at=INGESTED_AT)

    # Then
    actions = json.loads(records[0]["application_actions_json"])
    assert records[0]["strategic_market_id"] == "strategy_002"
    assert records[0]["question_text"] == "시장은?"
    assert records[0]["answer_text"] == "정의입니다."
    assert records[0]["source_remark"] == "검토 완료"
    assert actions["question_type"] == "시장 정의"


def test_load_qa_records_fails_closed_when_headers_collide_after_normalization(
    tmp_path: Path,
) -> None:
    # Given
    workbook_path = tmp_path / "master.xlsx"
    headers = BASE_HEADERS + ("\ufeff 질문 ",)
    values = BASE_VALUES + ("충돌 값",)
    _write_qa_workbook(workbook_path, headers, values)

    # When / Then
    with pytest.raises(ValueError, match=r"normalized header collision.*질문"):
        load_qa_records(workbook_path, ingested_at=INGESTED_AT)
