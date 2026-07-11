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
