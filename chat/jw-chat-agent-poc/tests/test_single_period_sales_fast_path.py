from __future__ import annotations

from typing import Any, TypedDict

from jw_chat_agent_poc.orchestrator.answer_facts import answer_fact_markdown
from jw_chat_agent_poc.orchestrator.answer_completeness import (
    deterministic_single_period_sales_answer,
)
from jw_chat_agent_poc.common.timing import _public_stage_detail, _public_stage_name
from jw_chat_agent_poc.service.genos_client import GenosClient


QUESTION = "리바로 2025년 2분기 매출"
FACT_MD = """### 리바로 지표 fact
| 항목 | 값 |
| --- | --- |
| 브랜드/시장 | 리바로 |
| 지표 | sales |
| 기간 | 2025-Q2 |
| 매출 | 242.72억원 |

### 리바로 매출 시계열 fact
| 기간 | 매출 | MS |
| --- | --- | --- |
| 2025-Q1 | 228.14억원 | 4.81% |
| 2025-Q2 | 242.72억원 | 4.92% |

### provenance fact
| 출처 | 기준기간 | 뷰 | 시장정의 | 분모 | 채널 | 단위 |
| --- | --- | --- | --- | --- | --- | --- |
| UBIST | 2025-Q2 | 전략뷰 | 요청 브랜드의 전략 시장 | 555 | 전체 | 억원 |
"""


class ToolCall(TypedDict):
    tool: str
    source: str
    render_data: dict[str, Any]


class MarkdownResponse(TypedDict):
    fact_md: str
    data_md: str
    allowed_numbers: tuple[()]


def _sales_call(*, brand: str = "리바로", period: str = "2025-Q2", sales: float = 242.72) -> ToolCall:
    return {
        "tool": "get_brand_metric",
        "source": "UBIST",
        "render_data": {
            "status": "ok",
            "brand": brand,
            "metric": "sales",
            "period": period,
            "sales_억원": sales,
            "query_spec": {
                "view": "market_landscape",
                "filters": {"brand": brand, "period": period},
                "total_brands_in_market": 555,
            },
        },
    }


def _markdown_response(fact_md: str = FACT_MD) -> MarkdownResponse:
    return {"fact_md": fact_md, "data_md": fact_md, "allowed_numbers": ()}


def test_single_period_sales_fast_path_renders_only_verified_surface() -> None:
    answer = deterministic_single_period_sales_answer(QUESTION, FACT_MD, [_sales_call()])

    assert answer == "2025-Q2 리바로 매출은 242.72억원입니다."


def test_single_period_sales_fast_path_accepts_the_production_fact_renderer() -> None:
    fact_md = answer_fact_markdown([_sales_call()], ["UBIST"])

    answer = deterministic_single_period_sales_answer(QUESTION, fact_md, [_sales_call()])

    assert answer == "2025-Q2 리바로 매출은 242.72억원입니다."


def test_single_period_sales_fast_path_preserves_raw_monthly_precision() -> None:
    call = _sales_call(period="2025-04", sales=83.184115)
    call["render_data"]["sales_krw"] = 8_318_411_526.5
    fact_md = answer_fact_markdown([call], ["UBIST"])

    answer = deterministic_single_period_sales_answer(
        "리바로 2025년 4월 매출",
        fact_md,
        [call],
    )

    assert "| 매출 | 83.18억원 |" in fact_md
    assert answer == "2025-04 리바로 매출은 83.184115억원입니다."


def test_single_period_sales_fast_path_keeps_quarter_at_two_decimals() -> None:
    call = _sales_call()
    call["render_data"]["sales_krw"] = 24_272_468_115.55
    fact_md = answer_fact_markdown([call], ["UBIST"])

    answer = deterministic_single_period_sales_answer(QUESTION, fact_md, [call])

    assert answer == "2025-Q2 리바로 매출은 242.72억원입니다."


def test_single_period_sales_fast_path_checks_later_duplicate_metric_fact() -> None:
    fact_md = """### 리바로 지표 fact
| 항목 | 값 |
| --- | --- |
| 브랜드/시장 | 리바로 |
| 지표 | query_spec |
| 기간 | 2026-05 |

### 리바로 지표 fact
| 항목 | 값 |
| --- | --- |
| 브랜드/시장 | 리바로 |
| 지표 | sales |
| 기간 | 2025-Q2 |
| 매출 | 242.72억원 |
"""

    answer = deterministic_single_period_sales_answer(QUESTION, fact_md, [_sales_call()])

    assert answer == "2025-Q2 리바로 매출은 242.72억원입니다."


def test_single_period_sales_stage_uses_a_user_facing_label() -> None:
    assert _public_stage_name("final_deterministic_single_period_sales_path") == "답변 작성"
    assert (
        _public_stage_detail("verified single-period sales answer rendering")
        == "검증된 단일기간 매출 답변 조립"
    )


def test_stream_answer_bypasses_final_llm_and_keeps_provenance(monkeypatch) -> None:
    def unexpected_llm(*_args, **_kwargs) -> str:
        raise AssertionError("final LLM must not run for an exact single-period sales fact")

    monkeypatch.setattr(GenosClient, "_markdown_answer", unexpected_llm)
    timing = {"stages": []}

    answer = "".join(
        GenosClient(token="dummy-token").stream_answer(
            QUESTION,
            {
                "markdown_response": _markdown_response(),
                "tool_calls": [_sales_call()],
                "timing": timing,
            },
        )
    )

    assert answer.startswith("2025-Q2 리바로 매출은 242.72억원입니다.")
    assert "| UBIST | 2025-Q2 | 전략뷰 | 요청 브랜드의 전략 시장 | 555 | 전체 | 억원 |" in answer
    assert [item["name"] for item in timing["stages"]] == [
        "final_deterministic_single_period_sales_path"
    ]


def test_tool_use_agent_single_period_sales_still_bypasses_final_llm(monkeypatch) -> None:
    def unexpected_llm(*_args, **_kwargs) -> str:
        raise AssertionError("tool-use routing must not hide the verified sales fast path")

    monkeypatch.setattr(GenosClient, "_markdown_answer", unexpected_llm)

    answer = "".join(
        GenosClient(token="dummy-token").stream_answer(
            QUESTION,
            {
                "markdown_response": _markdown_response(),
                "tool_calls": [_sales_call()],
                "router_diagnostics": {"mode": "tool_use_agent"},
            },
        )
    )

    assert answer.startswith("2025-Q2 리바로 매출은 242.72억원입니다.")
    assert "| UBIST | 2025-Q2 |" in answer


def test_mismatched_fact_and_tool_use_existing_llm_path(monkeypatch) -> None:
    calls = 0

    def llm_path(*_args, **_kwargs) -> str:
        nonlocal calls
        calls += 1
        return "기존 LLM 경로"

    monkeypatch.setattr(GenosClient, "_markdown_answer", llm_path)
    answer = "".join(
        GenosClient(token="dummy-token").stream_answer(
            QUESTION,
            {
                "markdown_response": _markdown_response(),
                "tool_calls": [_sales_call(sales=999.99)],
            },
        )
    )

    assert answer == "기존 LLM 경로"
    assert calls == 1


def test_missing_period_uses_existing_llm_path(monkeypatch) -> None:
    calls = 0

    def llm_path(*_args, **_kwargs) -> str:
        nonlocal calls
        calls += 1
        return "기존 LLM 경로"

    monkeypatch.setattr(GenosClient, "_markdown_answer", llm_path)
    incomplete = _sales_call()
    incomplete["render_data"]["period"] = ""
    answer = "".join(
        GenosClient(token="dummy-token").stream_answer(
            QUESTION,
            {"markdown_response": _markdown_response(), "tool_calls": [incomplete]},
        )
    )

    assert answer == "기존 LLM 경로"
    assert calls == 1


def test_compare_or_trend_question_never_uses_single_period_fast_path() -> None:
    call = _sales_call()

    assert deterministic_single_period_sales_answer(
        "리바로와 로수젯 2025년 2분기 매출 비교", FACT_MD, [call]
    ) == ""
    assert deterministic_single_period_sales_answer(
        "리바로 2025년 2분기 매출 추이", FACT_MD, [call]
    ) == ""
    assert deterministic_single_period_sales_answer(
        "리바로 2025년 2분기 매출과 점유율", FACT_MD, [call]
    ) == ""
    assert deterministic_single_period_sales_answer(
        "리바로 대비 로수젯 2025년 2분기 매출", FACT_MD, [call]
    ) == ""
    assert deterministic_single_period_sales_answer(
        "리바로 2025년 2분기 매출 분석해줘", FACT_MD, [call]
    ) == ""
