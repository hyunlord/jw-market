from __future__ import annotations

import requests

from jw_chat_agent_poc.orchestrator.answer_completeness import deterministic_top_n_share_answer
from jw_chat_agent_poc.orchestrator.answer_contract import enforce_answer_contract, evaluate_answer_contract
from jw_chat_agent_poc.service.answer_safety import generation_attempts
from jw_chat_agent_poc.service.genos_client import GenosClient


QUESTION = "리바로 시장 상위 5개 브랜드 점유율과 합계를 알려줘"

TOP_FACT = """### 상위 브랜드 점유율 추이 fact
| 최신 순위 | 브랜드 | 시작 MS | 최신 MS | MS 변화 | 최신 매출 | 매출 변화 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 로수젯 | 2025-08 9.00% | 2026-05 9.13% | +0.13%p | 195.24억원 | +2.00억원 |
| 2 | 리피토 | 2025-08 6.30% | 2026-05 6.13% | -0.17%p | 131.09억원 | -3.00억원 |
| 3 | 리바로젯 | 2025-08 5.00% | 2026-05 5.12% | +0.12%p | 109.46억원 | +2.00억원 |
| 4 | 아토젯 | 2025-08 4.80% | 2026-05 4.95% | +0.15%p | 105.87억원 | +3.00억원 |
| 5 | 로수바미브 | 2025-08 4.10% | 2026-05 4.20% | +0.10%p | 89.76억원 | +2.00억원 |

### provenance fact
| 출처 | 기준기간 | 뷰 | 시장정의 | 분모 | 채널 | 단위 |
| --- | --- | --- | --- | --- | --- | --- |
| UBIST | 2026-05 | 전략뷰 (market_landscape) | 스타틴 시장 | 470 | 전체 | 억원, % |
"""

SEGMENTS = (
    (1, "로수젯", 9.1264939920, 19_523_856_225.95),
    (2, "리피토", 6.1277726065, 13_108_840_203.03),
    (3, "리바로젯", 5.1167179108, 10_945_941_007.16),
    (4, "아토젯", 4.9487627406, 10_586_642_836.56),
    (5, "로수바미브", 4.1960520158, 8_976_406_092.54),
)


def _top_call(limit: int = 5) -> dict:
    return {
        "tool": "get_brand_metric",
        "source": "UBIST",
        "render_data": {
            "status": "ok",
            "brand": "리바로",
            "metric": "market_top_brands",
            "period": "2026-05",
            "market_id": "ml_006",
            "market_name": "스타틴 시장",
            "source_label": "UBIST",
            "level": "Brand",
            "level_segments": [
                {
                    "rank": rank,
                    "name": brand,
                    "brand": brand,
                    "ms_recent_pct": share,
                    "value": sales,
                    "value_억원": round(sales / 100_000_000, 2),
                }
                for rank, brand, share, sales in SEGMENTS[:limit]
            ],
        },
    }


def _markdown_response(fact_md: str = TOP_FACT) -> dict:
    return {"fact_md": fact_md, "data_md": fact_md, "allowed_numbers": ()}


def test_top_n_share_fast_path_renders_verified_table_and_raw_sum() -> None:
    answer = deterministic_top_n_share_answer(QUESTION, TOP_FACT, [_top_call()])

    assert answer.startswith("상위 5개 합계 시장점유율은 29.52%입니다.")
    assert "| 순위 | 브랜드 | 점유율 | 매출 |" in answer
    assert "| 1위 | 로수젯 | 9.13% | 195.24억원 |" in answer
    assert "| 5위 | 로수바미브 | 4.20% | 89.76억원 |" in answer
    assert answer.count("억원 |") == 5


def test_stream_answer_bypasses_final_llm_and_emits_fast_path_marker(monkeypatch) -> None:
    def unexpected_llm(_self: GenosClient, _messages: list[dict[str, str]]) -> str:
        raise AssertionError("final LLM must not run for a complete top-N fact set")

    monkeypatch.setattr(GenosClient, "_chat_text", unexpected_llm)
    timing = {"stages": []}

    answer = "".join(
        GenosClient(token="dummy-token").stream_answer(
            QUESTION,
            {
                "markdown_response": _markdown_response(),
                "tool_calls": [_top_call()],
                "timing": timing,
            },
        )
    )

    assert answer.startswith("상위 5개 합계 시장점유율은 29.52%입니다.")
    assert "| 출처 | 기준기간 | 뷰 | 시장정의 | 분모 | 채널 | 단위 |" in answer
    assert [item["name"] for item in timing["stages"]] == ["final_deterministic_fast_path"]


def test_incomplete_fact_set_uses_existing_llm_path(monkeypatch) -> None:
    partial_fact = TOP_FACT.replace(
        "| 5 | 로수바미브 | 2025-08 4.10% | 2026-05 4.20% | +0.10%p | 89.76억원 | +2.00억원 |\n",
        "",
    )
    calls = 0

    def llm_path(_self: GenosClient, *_args, **_kwargs) -> str:
        nonlocal calls
        calls += 1
        return "기존 LLM 경로"

    monkeypatch.setattr(GenosClient, "_markdown_answer", llm_path)

    answer = "".join(
        GenosClient(token="dummy-token").stream_answer(
            QUESTION,
            {"markdown_response": _markdown_response(partial_fact), "tool_calls": [_top_call()]},
        )
    )

    assert answer == "기존 LLM 경로"
    assert calls == 1


def test_fact_and_tool_mismatch_uses_existing_llm_path(monkeypatch) -> None:
    mismatched_call = _top_call()
    mismatched_call["render_data"]["level_segments"][0]["ms_recent_pct"] = 9.99
    calls = 0

    def llm_path(_self: GenosClient, *_args, **_kwargs) -> str:
        nonlocal calls
        calls += 1
        return "기존 LLM 경로"

    monkeypatch.setattr(GenosClient, "_markdown_answer", llm_path)

    answer = "".join(
        GenosClient(token="dummy-token").stream_answer(
            QUESTION,
            {"markdown_response": _markdown_response(), "tool_calls": [mismatched_call]},
        )
    )

    assert answer == "기존 LLM 경로"
    assert calls == 1


def test_fast_path_is_byte_stable_through_answer_contract() -> None:
    fast = deterministic_top_n_share_answer(QUESTION, TOP_FACT, [_top_call()])

    revised = enforce_answer_contract(QUESTION, fast, _markdown_response())
    status = evaluate_answer_contract(QUESTION, revised, _markdown_response())

    assert revised == fast
    assert status["status"] == "pass"


def test_default_final_retry_budget_fits_client_deadline(monkeypatch) -> None:
    monkeypatch.delenv("GENOS_FINAL_TIMEOUT_S", raising=False)
    monkeypatch.delenv("GENOS_GENERATION_ATTEMPTS", raising=False)
    monkeypatch.delenv("GENOS_FINAL_TOTAL_BUDGET_S", raising=False)

    client = GenosClient(token="dummy-token")

    assert client.timeout_s == 50
    assert generation_attempts() == 2
    assert client.total_budget_s == 100
    assert client.timeout_s * generation_attempts() <= client.total_budget_s < 180


def test_final_timeout_fails_closed_after_bounded_attempts(monkeypatch) -> None:
    monkeypatch.setenv("GENOS_FINAL_TIMEOUT_S", "50")
    monkeypatch.setenv("GENOS_GENERATION_ATTEMPTS", "2")
    monkeypatch.setenv("GENOS_FINAL_TOTAL_BUDGET_S", "100")
    attempts = 0

    def timeout(_self: GenosClient, _messages: list[dict[str, str]]):
        nonlocal attempts
        attempts += 1
        raise requests.Timeout("serving tail")
        yield ""

    monkeypatch.setattr(GenosClient, "_stream_chat", timeout)
    answer = "".join(
        GenosClient(token="dummy-token").stream_answer(
            "리바로 최근 매출 알려줘",
            {"markdown_response": _markdown_response()},
        )
    )

    assert attempts == 2
    assert answer.strip()
    assert "Internal Server Error" not in answer
