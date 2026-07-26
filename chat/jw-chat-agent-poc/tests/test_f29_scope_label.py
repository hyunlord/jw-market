"""F29 RC2 public strategic-market scope label regressions."""

from __future__ import annotations

import re
from typing import TypedDict

from jw_chat_agent_poc.orchestrator.answer_facts import answer_fact_markdown
from jw_chat_agent_poc.orchestrator.provenance_model import public_view
from jw_chat_agent_poc.service.answer_safety import deterministic_source_block


class _QuerySpec(TypedDict):
    market: str
    market_name: str
    view: str
    total_brands_in_market: int


class _RenderData(TypedDict):
    source_label: str
    period: str
    metric: str
    query_spec: _QuerySpec


class _MarketCall(TypedDict):
    tool: str
    source: str
    render_data: _RenderData


def _market_call(
    market_id: str,
    market_name: str,
    denominator: int,
    *,
    period: str = "2026-05",
) -> _MarketCall:
    return {
        "tool": "get_brand_metric",
        "source": "UBIST",
        "render_data": {
            "source_label": "UBIST",
            "period": period,
            "metric": "sales",
            "query_spec": {
                "market": market_id,
                "market_name": market_name,
                "view": "market_landscape",
                "total_brands_in_market": denominator,
            },
        },
    }


def _source_block(calls: list[_MarketCall]) -> str:
    fact_md = answer_fact_markdown(calls, ["UBIST"])
    return deterministic_source_block(fact_md)


def test_distinct_strategic_markets_do_not_collapse_into_one_public_row() -> None:
    block = _source_block(
        [
            _market_call("ml_555", "요청 브랜드 전략시장", 555),
            _market_call("ml_566", "고지혈증", 566),
        ],
    )

    assert block.count("| UBIST |") == 2
    assert "전략뷰 (market_landscape) · 요청 브랜드 전략시장" in block
    assert "전략뷰 (market_landscape) · 고지혈증" in block
    assert "| 555, 566 |" not in block
    assert not re.search(r"\bml_(?:555|566)\b", block)


def test_rows_for_the_same_public_market_still_merge() -> None:
    block = _source_block(
        [
            _market_call("ml_555", "요청 브랜드 전략시장", 555, period="2026-04"),
            _market_call("ml_555", "요청 브랜드 전략시장", 555, period="2026-05"),
        ],
    )

    assert block.count("| UBIST |") == 1
    assert "2026-04~2026-05" in block
    assert "| 555 |" in block


def test_view_family_mapping_remains_unchanged_before_public_scope_labeling() -> None:
    assert public_view("general", "C10A1") == "일반뷰 (ATC4)"
    assert public_view("market_landscape", "ml_555") == "전략뷰 (market_landscape)"
    assert public_view("competitive_dynamics", "cd_555") == "전략뷰 (competitive_dynamics)"
