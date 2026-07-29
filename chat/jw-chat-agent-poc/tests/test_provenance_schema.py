from __future__ import annotations

import re

import pytest

from jw_chat_agent_poc.orchestrator.answer_facts import answer_fact_markdown
from jw_chat_agent_poc.orchestrator.markdown_formatting import allowed_numbers
from jw_chat_agent_poc.orchestrator.unavailable_response import sanitize_internal_diagnostics
from jw_chat_agent_poc.service.answer_safety import (
    append_deterministic_source_block,
    deterministic_source_block,
)
from jw_chat_agent_poc.service.markdown_cleanup import cleanup_markdown_answer


PROVENANCE_HEADER = "| 출처 | 기준기간 | 뷰 | 시장정의 | 분모 | 채널 | 단위 |"
INTERNAL_PROVENANCE_RE = re.compile(
    r"\b(?:ml|strategy|cd)_\d+\b|\btool_call_\d+\b|\bseries\b|확정 시장",
    re.IGNORECASE,
)


@pytest.mark.parametrize(
    "fact_md",
    (
        """### 수치별 출처 fact
| 수치 | 소스 | 기간 | 시장정의 | 축 | tool_call_id |
| --- | --- | --- | --- | --- | --- |
| 리바로 매출 84.93억원 | UBIST | 2026-04 | ml_006 | Brand | qr_0001 |
""",
        """### 출처 유형 fact
| 출처 | 상세 |
| --- | --- |
| 데이터 상세 | UBIST — 기간 2026-04 |
""",
        "",
    ),
    ids=("value_fact_table", "source_bullet", "no_source"),
)
def test_a1_all_provenance_families_render_one_seven_field_schema(fact_md: str) -> None:
    block = deterministic_source_block(fact_md)

    assert block.startswith("## 출처")
    assert block.count(PROVENANCE_HEADER) == 1
    assert "| 수치 | 소스 |" not in block
    assert "- 데이터:" not in block
    assert "- 데이터 상세:" not in block


def test_a2_missing_cells_never_shift_fallback_identifiers_into_public_columns() -> None:
    fact_md = """### 수치별 출처 fact
| 수치 | 소스 | 기간 | 시장정의 | 축 | tool_call_id |
| --- | --- | --- | --- | --- | --- |
| 로수젯 매출 206.85억원 | UBIST | - | - | series | tool_call_1 |

### 출처 유형 fact
| 출처 | 상세 |
| --- | --- |
| 데이터 상세 | UBIST — 시장: strategy_006 (market_landscape) |
"""

    block = deterministic_source_block(fact_md)

    assert not INTERNAL_PROVENANCE_RE.search(block)
    assert "| UBIST | — | — | — | — | 전체 | — |" in block


@pytest.mark.parametrize(
    ("market_id", "view", "market_name", "expected_view"),
    (
        ("ml_006", "market_landscape", "리바로/리바로젯 시장", "전략뷰 (market_landscape)"),
        ("cd_006", "competitive_dynamics", "리바로 경쟁군", "전략뷰 (competitive_dynamics)"),
        ("C10A1", "general", "고지혈증 시장", "일반뷰 (ATC4)"),
    ),
)
def test_a3_internal_market_ids_map_to_public_market_labels(
    market_id: str,
    view: str,
    market_name: str,
    expected_view: str,
) -> None:
    fact_md = answer_fact_markdown(
        [
            {
                "tool": "get_brand_metric",
                "source": "UBIST",
                "render_data": {
                    "source_label": "UBIST",
                    "brand": "리바로",
                    "metric": "sales",
                    "period": "2026-04",
                    "sales_억원": 84.93,
                    "query_spec": {
                        "market": market_id,
                        "market_name": market_name,
                        "view": view,
                        "total_brands_in_market": 470,
                    },
                },
            }
        ],
        ["UBIST"],
    )

    block = deterministic_source_block(fact_md)

    assert expected_view in block
    assert market_name in block
    assert "| 470 | 전체 | 억원 |" in block
    assert not INTERNAL_PROVENANCE_RE.search(block)
    if view == "market_landscape":
        assert "일반뷰" not in block


def test_file_source_uses_the_same_seven_field_renderer() -> None:
    block = deterministic_source_block(
        "",
        file_context="[1] qa_e2e_operations_brief.docx\n승인코드: NAR-7712",
    )

    assert PROVENANCE_HEADER in block
    assert "업로드 파일(qa_e2e_operations_brief.docx)" in block
    assert "| — | 파일 | — | — | 전체 | — |" in block
    assert "- 업로드 파일:" not in block


def test_multi_file_sources_render_one_row_per_file() -> None:
    block = deterministic_source_block(
        "",
        file_context=(
            "[1] pdrn_survey.xlsx\n성별 변수 SQ1과 연령 변수 SQ2가 있다.\n\n"
            "[2] dyslipidemia_di.xlsx\nCVOT와 LDL-C 강하 효과 항목이 있다."
        ),
    )

    assert block.count("업로드 파일(pdrn_survey.xlsx)") == 1
    assert block.count("업로드 파일(dyslipidemia_di.xlsx)") == 1


def test_i1_final_cleanup_blocks_every_internal_provenance_label() -> None:
    raw = (
        "## 출처\n"
        "| 출처 | 기준기간 | 뷰 | 시장정의 | 분모 | 채널 | 단위 |\n"
        "| UBIST | 확정 시장 | market_landscape | ml_006 strategy_006 cd_006 | 470 | series | tool_call_1 |"
    )

    cleaned = cleanup_markdown_answer(raw)

    assert not INTERNAL_PROVENANCE_RE.search(cleaned)


def test_r1_fallback_sanitizer_does_not_rewrite_answer_body_terms() -> None:
    raw = (
        "리바로 series 값과 확정 시장 설명은 답변 본문입니다.\n\n"
        "## 출처\n"
        "| 출처 | 기준기간 | 뷰 | 시장정의 | 분모 | 채널 | 단위 |\n"
        "| UBIST | 확정 시장 | market_landscape | ml_006 | 470 | series | tool_call_1 |"
    )

    cleaned = cleanup_markdown_answer(raw)
    body, provenance = cleaned.split("## 출처", 1)

    assert "series 값과 확정 시장 설명" in body
    assert not INTERNAL_PROVENANCE_RE.search(provenance)


def test_unavailable_sanitizer_does_not_create_internal_fallback_labels() -> None:
    sanitized = sanitize_internal_diagnostics(
        "| UBIST | 2026-04 | market_landscape | ml_006 | 470 | 전체 | 억원 |"
    )

    assert "확정 시장" not in sanitized
    assert not re.search(r"\bml_\d+\b", sanitized)


def test_r1_provenance_rewrite_preserves_answer_body_numbers() -> None:
    answer = "상위 5개 합계 시장점유율은 30.33%입니다.\n\n## 출처\n- 데이터: UBIST"
    fact_md = """### provenance fact
| 출처 | 기준기간 | 뷰 | 시장정의 | 분모 | 채널 | 단위 |
| --- | --- | --- | --- | --- | --- | --- |
| UBIST | 2026-04 | 전략뷰 (market_landscape) | 리바로/리바로젯 시장 | 470 | 전체 | % |
"""

    before_body = answer.split("## 출처", 1)[0].strip()
    revised = append_deterministic_source_block(answer, fact_md)
    after_body = revised.split("## 출처", 1)[0].strip()

    assert after_body == before_body
    assert allowed_numbers(after_body) == allowed_numbers(before_body)
    assert "30.33%" in revised


def test_same_source_and_view_merge_period_and_multi_value_fields() -> None:
    fact_md = """### provenance fact
| 출처 | 기준기간 | 뷰 | 시장정의 | 분모 | 채널 | 단위 |
| --- | --- | --- | --- | --- | --- | --- |
| UBIST | 2026-04 | 전략뷰 (market_landscape) | 리바로/리바로젯 | 470 | 전체 | 억원 |
| UBIST | 2025-07~2026-03 | 전략뷰 (market_landscape) | 리바로/리바로젯 | 516 | 의원 | % |
"""

    block = deterministic_source_block(fact_md)

    assert block.count("| UBIST |") == 1
    assert (
        "| UBIST | 2025-07~2026-04 | 전략뷰 (market_landscape) · 리바로/리바로젯 | "
        "리바로/리바로젯 | 470, 516 | 의원, 전체 | %, 억원 |"
    ) in block


def test_source_and_view_are_strict_group_boundaries() -> None:
    fact_md = """### provenance fact
| 출처 | 기준기간 | 뷰 | 시장정의 | 분모 | 채널 | 단위 |
| --- | --- | --- | --- | --- | --- | --- |
| UBIST | 2026-04 | 전략뷰 (market_landscape) | — | 470 | 전체 | 억원 |
| UBIST | 2026-04 | 일반뷰 (ATC4) | ATC4 C10A1 | 516 | 전체 | 억원 |
| IQVIA NSA | 2025-Q4 | 전략뷰 (market_landscape) | — | — | 전체 | 억원 |
"""

    block = deterministic_source_block(fact_md)

    assert block.count("| UBIST |") == 2
    assert block.count("| IQVIA NSA |") == 1
    assert "전략뷰 (market_landscape)" in block
    assert "일반뷰 (ATC4)" in block


def test_multi_value_limit_and_separator_are_environment_driven(monkeypatch) -> None:
    monkeypatch.setenv("JW_CHAT_PROVENANCE_MULTI_VALUE_LIMIT", "3")
    monkeypatch.setenv("JW_CHAT_PROVENANCE_MULTI_VALUE_SEPARATOR", " / ")
    fact_md = """### provenance fact
| 출처 | 기준기간 | 뷰 | 시장정의 | 분모 | 채널 | 단위 |
| --- | --- | --- | --- | --- | --- | --- |
| UBIST | 2026-01 | 전략뷰 (market_landscape) | A | 470 | 전체 | 억원 |
| UBIST | 2026-02 | 전략뷰 (market_landscape) | A | 516 | 의원 | % |
| UBIST | 2026-03 | 전략뷰 (market_landscape) | A | 600 | 병원 | 위 |
| UBIST | 2026-04 | 전략뷰 (market_landscape) | A | 700 | 약국 | 명 |
"""

    block = deterministic_source_block(fact_md)

    assert block.count("| UBIST |") == 1
    assert "| 전략뷰 (market_landscape) · A | A |" in block
    assert "| 470 / 516 외 2 |" in block
    assert "| 병원 / 약국 외 2 |" in block
    assert "| % / 명 외 2 |" in block


def test_merging_public_source_rows_does_not_modify_answer_body() -> None:
    answer = "합산 점유율은 30.33%입니다."
    fact_md = """### provenance fact
| 출처 | 기준기간 | 뷰 | 시장정의 | 분모 | 채널 | 단위 |
| --- | --- | --- | --- | --- | --- | --- |
| UBIST | 2026-03 | 전략뷰 (market_landscape) | — | 470 | 전체 | 억원 |
| UBIST | 2026-04 | 전략뷰 (market_landscape) | — | 470 | 전체 | % |
"""

    revised = append_deterministic_source_block(answer, fact_md)

    assert revised.split("## 출처", 1)[0].strip() == answer
    assert "30.33%" in revised


def test_external_tool_bullets_preserve_actual_source_identity() -> None:
    fact_md = """- pitavastatin: 글로벌 임상시험 = NCT00257686 · Study to Compare Pitavastatin and Pravastatin · https://clinicaltrials.gov/study/NCT00257686 [ClinicalTrials.gov 임상시험 정보]
- 고지혈증 (20120118): 국내 임상시험 = HL040XC정 [식약처 의약품 정보]
- pitavastatin: 이상반응 = myalgia [FDA 이상반응 보고 정보]
- 고지혈증: 환자수 = 1,305,727명 [건강보험심사평가원 통계]
- 고지혈증 임상 현황: 웹 검색 = [국내진출 임박한 고지혈증 신약](https://example.test/clinical) [웹 검색 결과]
"""

    block = deterministic_source_block(fact_md)

    assert "| ClinicalTrials.gov |" in block
    assert "| 식약처 의약품안전나라(NeDrug) |" in block
    assert "| FDA 이상반응 보고 정보 |" in block
    assert "| 심사평가원(HIRA) 질병통계 |" in block
    assert "| 뉴스/이슈 「국내진출 임박한 고지혈증 신약」 https://example.test/clinical |" in block
    assert "| external |" not in block


def test_brand_column_stays_in_lockstep_with_the_row_tuple() -> None:
    """A header added without extending as_tuple() (or the reverse) misaligns every cell."""

    from jw_chat_agent_poc.orchestrator.provenance_model import (
        LEGACY_PROVENANCE_HEADERS,
        PROVENANCE_HEADERS,
        ProvenanceRow,
    )

    row = ProvenanceRow()
    assert len(PROVENANCE_HEADERS) == len(row.as_tuple()) == len(row.as_fields())
    # The brand column is appended, so no pre-existing column may shift.
    assert PROVENANCE_HEADERS[: len(LEGACY_PROVENANCE_HEADERS)] == LEGACY_PROVENANCE_HEADERS
    assert PROVENANCE_HEADERS[-1] == "브랜드"
    assert tuple(row.as_fields()) == tuple(ProvenanceRow.__dataclass_fields__)


def test_brand_separates_source_rows_but_still_dedupes_within_a_brand() -> None:
    from jw_chat_agent_poc.orchestrator.provenance_model import merge_public_source_rows, normalized_row

    def row(brand: str):
        return normalized_row(source="ubist", period="2026-05", denominator="555", brand=brand)

    same_brand = merge_public_source_rows([row("리바로"), row("리바로")])
    different_brands = merge_public_source_rows([row("리바로"), row("아토젯")])

    assert len(same_brand) == 1
    assert len(different_brands) == 2
    assert {item.brand for item in different_brands} == {"리바로", "아토젯"}


def test_intermediate_and_final_renderers_keep_brands_in_separate_rows() -> None:
    from jw_chat_agent_poc.orchestrator.market_answer_contract import enforce_market_answer_contract
    from jw_chat_agent_poc.orchestrator.provenance_labels import provenance_source_block

    calls = [
        {
            "tool": "get_brand_metric",
            "source": "UBIST",
            "render_data": {
                "status": "ok",
                "brand": brand,
                "metric": "share",
                "period": "2026-05",
                "market_name": "고지혈증 시장",
                "view_type": "market_landscape",
                "rank_denominator": 555,
                "ms_recent_pct": share,
            },
        }
        for brand, share in (("리바로", 3.76), ("아토젯", 4.12))
    ]

    intermediate = provenance_source_block(calls, ["UBIST"])
    final = enforce_market_answer_contract(
        "리바로와 아토젯의 점유율 변화 비교",
        "비교 결과입니다.",
        calls,
    )

    for rendered in (intermediate, final):
        assert rendered.count("| 리바로 |") == 1
        assert rendered.count("| 아토젯 |") == 1
        assert "리바로, 아토젯" not in rendered


def test_legacy_seven_column_provenance_fact_still_parses_field_for_field() -> None:
    """Tables rendered before the brand column existed must not shift by one."""

    from jw_chat_agent_poc.orchestrator.provenance_facts import provenance_rows_from_fact_markdown

    legacy = (
        "### provenance fact\n"
        f"{PROVENANCE_HEADER}\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        "| UBIST | 2026-05 | 전략뷰 | 고지혈증 | 555 | 전체 | 억원 |\n"
    )
    (parsed,) = provenance_rows_from_fact_markdown(legacy)
    assert parsed.source == "UBIST"
    assert parsed.period == "2026-05"
    assert parsed.denominator == "555"
    assert parsed.unit == "억원"
    assert parsed.brand == "—"
