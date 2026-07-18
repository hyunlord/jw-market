from __future__ import annotations

from pathlib import Path
import re

import pytest

from jw_chat_agent_poc.orchestrator.unavailable_response import apply_common_unavailable_response
from jw_chat_agent_poc.service import app, file_sql_query
from jw_chat_agent_poc.service.markdown_cleanup import scrub_internal_terminology


def _chso_schema(*, include_atc4: bool = True) -> dict:
    columns = [
        {"query_name": "c2", "source_name": "MFR NAME KOR"},
        {"query_name": "c72", "source_name": "VALUES LC SI PRICE 1/2026"},
    ]
    if include_atc4:
        columns.insert(1, {"query_name": "c12", "source_name": "ATC 4"})
    return {
        "logical_name": "doc-91:sheet-1",
        "file_name": "CHSO.xlsx",
        "sheet_name": "Sell Out Standard",
        "columns": columns,
    }


def _source() -> file_sql_query.SqlFileSource:
    return file_sql_query.SqlFileSource(
        "doc-91:sheet-1", "CHSO.xlsx", "Sell Out Standard"
    )


def test_file_query_without_grounded_plan_fails_closed_without_llm(monkeypatch) -> None:
    monkeypatch.setattr(file_sql_query, "_fetch_schema", lambda *_args: _chso_schema())

    outcome = file_sql_query.query_uploaded_sql(
        "지역별 재구매 고객군의 이탈 위험을 분석해줘",
        "conversation-1",
        (_source(),),
    )

    assert outcome.status == "unsupported_query"
    assert "이 파일에서 요청한 조건을 찾을 수 없습니다" in outcome.answer_md
    assert "열 이름을 확인해 주세요" in outcome.answer_md
    assert "추정할 수 없습니다" not in outcome.answer_md
    assert "확보되어야" not in outcome.answer_md


def test_broad_sellout_analysis_asks_for_a_grounded_dimension(monkeypatch) -> None:
    monkeypatch.setattr(
        file_sql_query,
        "_fetch_schema",
        lambda *_args: _chso_schema(),
    )

    outcome = file_sql_query.query_uploaded_sql(
        "셀아웃 데이터 분석해줘",
        "conversation-1",
        (_source(),),
    )

    assert outcome.status == "unsupported_query"
    assert "어떤 기준으로 분석할까요?" in outcome.answer_md
    assert "2026년 1월 총 sell-out 금액" in outcome.answer_md
    assert "제조사별 합계" in outcome.answer_md
    assert "파일의 열 이름을 확인" not in outcome.answer_md


def test_legacy_llm_sql_generation_seam_is_structurally_disabled() -> None:
    with pytest.raises(RuntimeError, match="LLM file SQL generation is disabled"):
        file_sql_query._generate_select("자유 SQL을 만들어줘", (_chso_schema(),))


def test_missing_atc4_reports_only_unresolved_slot(monkeypatch) -> None:
    monkeypatch.setattr(
        file_sql_query,
        "_fetch_schema",
        lambda *_args: _chso_schema(include_atc4=False),
    )
    outcome = file_sql_query.query_uploaded_sql(
        "ATC4 A02B2에서 동아제약과 동화약품의 sell-out 금액 비교",
        "conversation-1",
        (_source(),),
    )

    assert outcome.status == "unsupported_query"
    assert "ATC4 관련 열이 없습니다" in outcome.answer_md
    assert "sell-out" not in outcome.answer_md.casefold()
    assert outcome.trace[-1]["missing_slots"] == "ATC4"
    assert "measure" in outcome.trace[-1]["resolved_slots"]


def test_zero_row_atc4_answer_does_not_gain_false_missing_sentences(monkeypatch) -> None:
    question = "ATC4 A02B2에서 동아제약과 동화약품의 sell-out 금액 비교"
    answer = "ATC4 열은 있으나 'A02B2' 조건에 맞는 행이 0건입니다."
    file_context = (
        "업로드 문서에서 ATC4 A02B2을(를) 찾을 수 없습니다.\n\n"
        "## 업로드 파일 SQL 결과\n상태: 조건 일치 0건\n" + answer
    )

    rendered = app.compute_final_answer(
        question,
        {
            "answer": answer,
            "deterministic_file_answer": answer,
            "file_context": file_context,
            "context_scope": "FILE",
            "sources": ["document"],
            "tool_calls": [],
        },
        "conversation-1",
    ).text

    false_missing = re.compile(
        r"^(?:업로드 문서에서\s+)?(?:ATC4\s+A02B2|sell-out)(?:을|를|은|는|이|가|의|을\(를\))?\s*찾을 수 없습니다[.!]?$",
        re.IGNORECASE,
    )
    sentences = tuple(part.strip() for part in re.split(r"(?<=[.!?])\s+|\n+", rendered) if part.strip())
    assert re.fullmatch(
        r"ATC4 열은 있으나 'A02B2' 조건에 맞(?:는|은) 행이 0건입니다\.",
        sentences[0],
    )
    assert not any(false_missing.fullmatch(sentence) for sentence in sentences)


def test_bare_file_aggregate_without_prior_slots_fails_closed() -> None:
    resolution = file_sql_query._resolve_deterministic_select("합계는?", (_chso_schema(),))

    assert resolution.plan is None
    assert resolution.missing_slots == ("집계 대상",)
    assert file_sql_query._missing_plan_answer(resolution.missing_slots) == (
        "무엇의 합계인지 명확하지 않습니다. 제조사 또는 측정 항목을 지정해 주세요."
    )


@pytest.mark.parametrize(
    ("question", "expected_sql"),
    [
        ("2026년 1월 총 sell-out 금액은?", "SELECT SUM(c72) AS total_value"),
        ("동아제약의 sell-out 합계는?", "c2 = '동아제약'"),
    ],
)
def test_common_file_aggregates_have_deterministic_plans(question: str, expected_sql: str) -> None:
    resolution = file_sql_query._resolve_deterministic_select(question, (_chso_schema(),))

    assert resolution.plan is not None
    assert expected_sql in resolution.plan["sql"]
    assert resolution.missing_slots == ()


@pytest.mark.parametrize(
    ("question", "expected_filter"),
    [
        ("A02B2에서 sell-out 금액은?", "c12 = 'A02B2'"),
        ("ATC4 A02B2에서 sell-out 금액은?", "c12 = 'A02B2'"),
    ],
)
def test_atc4_only_aggregate_does_not_invent_manufacturer_filter(
    question: str,
    expected_filter: str,
) -> None:
    resolution = file_sql_query._resolve_deterministic_select(question, (_chso_schema(),))

    assert resolution.plan is not None
    assert expected_filter in resolution.plan["sql"]
    assert "c2 = 'A02B2'" not in resolution.plan["sql"]


def test_atc4_and_manufacturer_aggregate_keeps_both_independent_filters() -> None:
    resolution = file_sql_query._resolve_deterministic_select(
        "ATC4 A02B2에서 동아제약의 sell-out 합계는?",
        (_chso_schema(),),
    )

    assert resolution.plan is not None
    assert "c12 = 'A02B2'" in resolution.plan["sql"]
    assert "c2 = '동아제약'" in resolution.plan["sql"]


def test_file_unavailable_contract_does_not_add_market_only_copy() -> None:
    answer = "이 파일에는 ATC4 관련 열이 없습니다. 파일의 열 이름을 확인해 주세요."

    rendered = apply_common_unavailable_response(
        "ATC4 A02B2에서 두 제조사를 비교해줘",
        answer,
        {"fact_md": ""},
        tool_calls=(),
        source_scope="FILE",
    )

    assert rendered == answer
    assert "general_view_unavailable" not in rendered
    assert "시장 도구" not in rendered
    assert "미보유 데이터 처리" not in rendered


@pytest.mark.parametrize(
    ("extension", "internal"),
    [
        ("xlsx", "TEMP_DOCUMENT_1845.xlsx"),
        ("csv", "document_id=113292"),
        ("docx", "temp_document_id: 991"),
        ("pptx", "vdb_id=235"),
        ("pdf", "chunk_id: abc-123"),
        ("txt", "tool_call_id=call-7"),
    ],
)
def test_all_file_internal_identifiers_are_scrubbed(extension: str, internal: str) -> None:
    answer = scrub_internal_terminology(f"근거 파일: report.{extension} | {internal} | 확인된 내용")

    assert internal not in answer
    assert not any(
        marker in answer
        for marker in (
            "TEMP_DOCUMENT",
            "document_id",
            "temp_document_id",
            "vdb_id",
            "chunk_id",
            "tool_call_id",
        )
    )


def test_runtime_package_has_no_retired_cause_cache_reference() -> None:
    package_root = Path(__file__).resolve().parents[1] / "jw_chat_agent_poc"

    matches = [
        path.relative_to(package_root)
        for path in package_root.rglob("*.py")
        if "cache_cause" in path.read_text(encoding="utf-8")
    ]

    assert matches == []


def test_public_file_sources_exclude_internal_identifiers() -> None:
    result = {
        "file_source_items": [
            {
                "file_name": "brief.pptx",
                "document_id": 113292,
                "temp_document_id": 991,
                "vdb_id": 235,
                "chunk_id": "chunk-1",
                "tool_call_id": "call-7",
                "i_page": 3,
                "slide_number": 3,
                "section_title": "시장 전망",
            }
        ]
    }

    assert app._file_source_items(result) == (
        {
            "file_name": "brief.pptx",
            "i_page": 3,
            "slide_number": 3,
            "section_title": "시장 전망",
        },
    )


@pytest.mark.parametrize("extension", ("xlsx", "csv", "docx", "pptx", "pdf", "txt"))
def test_structured_file_sources_scrub_internal_file_names(extension: str) -> None:
    result = {
        "file_source_items": [
            {
                "file_name": f"TEMP_DOCUMENT_1845.{extension}",
                "i_page": 1,
            }
        ]
    }

    assert app._file_source_items(result) == ({"file_name": "업로드 문서", "i_page": 1},)


def test_file_only_ready_answer_uses_final_identifier_scrub() -> None:
    final = app.compute_final_answer(
        "",
        {
            "file_only_ready": True,
            "answer": "TEMP_DOCUMENT_1845.pptx document_id=113292 업로드 완료",
            "sources": ["document"],
        },
        "conversation-1",
    )

    assert "TEMP_DOCUMENT" not in final.text
    assert "document_id" not in final.text
