"""P0-B: single internal-terminology scrub gate.

RED before the fix: cleanup_markdown_answer lets internal tool names, query ids,
internal fact headings, the verifier notice and "agent loop" wording reach the
user. GREEN after: they are scrubbed, while brand/market names and ordinary
Korean prose (including the bare word 주의) are preserved (오탐 0).
"""
from __future__ import annotations

import pytest

from jw_chat_agent_poc.service.markdown_cleanup import cleanup_markdown_answer

TOOL_TOKENS = (
    "get_brand_metric",
    "get_metric",
    "get_market_scope",
    "resolve_relative_date",
    "search_news",
    "get_disease_stats",
    "get_procedure_stats",
    "search_clinical",
    "search_patent",
    "search_drug_info",
    "csd_activity_trend",
    "get_csd_activity_trend",
    "web_search",
    "get_brand_sales",
    "get_brand_share",
    "get_brand_series",
    "compare_brands_series",
    "get_top_brands",
    "get_brand_channel_breakdown",
    "get_brand_specialty_breakdown",
)


@pytest.mark.parametrize("token", TOOL_TOKENS)
def test_gate_scrubs_tool_names(token: str) -> None:
    for template in ("{t} 도구로 조회했습니다.", "{t} 결과가 없습니다.", "{t} 를 실행하지 못했습니다."):
        out = cleanup_markdown_answer(template.format(t=token))
        assert token not in out, f"{token} leaked via {template!r}: {out!r}"


def test_gate_scrubs_query_identifiers() -> None:
    assert "qr_0001" not in cleanup_markdown_answer("query(spec) qr_0001를 전략 mart에서 실행했습니다.")
    assert "query_result_id" not in cleanup_markdown_answer("query_result_id: 7f3a 를 참고했습니다.")
    assert "query(spec)" not in cleanup_markdown_answer("query(spec) 를 실행했습니다.")


def test_gate_scrubs_internal_fact_headings() -> None:
    assert "provenance fact" not in cleanup_markdown_answer("### provenance fact\n\n| 출처 | 값 |\n| --- | --- |")
    assert "필수 답변 fact" not in cleanup_markdown_answer("### 필수 답변 fact\n\n확정된 수치입니다.")
    assert "확정 fact set" not in cleanup_markdown_answer("## 확정 fact set\n\n확정된 수치입니다.")
    assert "fact set" not in cleanup_markdown_answer("확정 fact set을 유지합니다.")


def test_gate_scrubs_verifier_notice() -> None:
    out = cleanup_markdown_answer(
        "숫자 검증: 근거 표에 없는 숫자 표현을 감지해 해석을 확정 데이터 기준으로 제한했습니다."
    )
    assert "숫자 검증" not in out
    assert "근거 표" not in out


def test_gate_scrubs_agent_loop_wording() -> None:
    for raw in (
        "반복 도구 호출을 감지해 agent loop를 중단하고 확인된 도구 결과만 표시했습니다.",
        "agent loop step 예산을 초과해 확인된 도구 결과만 표시했습니다.",
    ):
        out = cleanup_markdown_answer(raw)
        assert "agent loop" not in out
        assert "agent_loop" not in out


# ----- false-positive guards (오탐 0): must remain intact -----

@pytest.mark.parametrize(
    "brand_line",
    (
        "리바로 점유율은 1위입니다.",
        "로수젯 매출 추이를 보여드립니다.",
        "리바로젯 시장 규모입니다.",
        "가드렛 매출은 증가했습니다.",
        "베노훼럼 시계열입니다.",
    ),
)
def test_gate_preserves_brand_names(brand_line: str) -> None:
    brand = brand_line.split()[0]
    assert brand in cleanup_markdown_answer(brand_line)


def test_gate_preserves_ordinary_prose_juui() -> None:
    out = cleanup_markdown_answer("해석에 주의가 필요합니다.")
    assert "주의가 필요합니다" in out


def test_gate_scrub_is_idempotent() -> None:
    raw = "search_news 결과와 query(spec) qr_0001, agent loop 중단 상황입니다."
    once = cleanup_markdown_answer(raw)
    twice = cleanup_markdown_answer(once)
    assert once == twice
