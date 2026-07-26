"""F40 regressions derived from the rolled-back feature/chat live gate.

The gate artifacts preserve the post-binding Markdown and, for the member
listing, the chart projection of ``render_data.level_segments``. They do not
serialize the complete tool payload. The payloads below therefore combine only
those observed values with the production producer schema; unlike the F24
fixture, they do not invent a shared number across unrelated scopes.

Sources:
- gate_g2_fixes/01_rc1_sales.json
  sha256 b02e46a59e383a20c934fa14ed3c8a42020cfffe4a21ca98cdab4e8c88f1d1f5
- gate_g2_fixes/04_rc3_anaphora_1.json
  sha256 18af0d696dabf9dcefb234458d70731c9719e8354d96c4a1a26df05b9c104336
"""

from __future__ import annotations

from jw_chat_agent_poc.orchestrator.provenance import evidence_from_calls
from jw_chat_agent_poc.service.evidence_binding import verify_claim_bindings
from jw_chat_agent_poc.service.app import _apply_evidence_binding_gate


_SUBJECT_SERIES = {
    "2025-08": 7_963_000_000.0,
    "2025-09": 8_929_000_000.0,
    "2025-10": 7_823_000_000.0,
    "2025-11": 8_035_000_000.0,
    "2025-12": 9_086_000_000.0,
    "2026-01": 8_303_000_000.0,
    "2026-02": 7_508_000_000.0,
    "2026-03": 8_711_000_000.0,
    "2026-04": 8_493_000_000.0,
    "2026-05": 8_039_000_000.0,
}

_MEMBERS = (
    ("로수젯", 9.126493992011786, 195.24),
    ("리피토", 6.127772606529449, 131.09),
    ("리바로젯", 5.116717910777399, 109.46),
    ("아토젯", 4.948762740580882, 105.87),
    ("로수바미브", 4.1960520158172825, 89.76),
)


def _live_sales_call() -> dict:
    return {
        "source": "UBIST",
        "tool": "get_brand_metric",
        "render_data": {
            "brand": "리바로",
            "metric": "series",
            "measure": "sales",
            "period": "2026-05",
            "view_type": "market_landscape",
            "market_id": "ml_555",
            # This legacy ordinary-brand series is the exact fallback input
            # identified by F37 at provenance.py:_latest_market_size.
            "series": dict(_SUBJECT_SERIES),
            "brand_value_series_10pt": [
                {
                    "period": period,
                    "value_krw": value,
                    "value_억원": value / 100_000_000,
                }
                for period, value in _SUBJECT_SERIES.items()
            ],
            "series_insight": {
                "competitors": [
                    {
                        "brand": brand,
                        "sales_end_krw": sales * 100_000_000,
                    }
                    for brand, _share, sales in _MEMBERS
                ],
            },
        },
    }


def _live_member_call() -> dict:
    return {
        "source": "UBIST",
        "tool": "get_market_members",
        "render_data": {
            "market": "ml_555",
            "market_id": "ml_555",
            "view_type": "market_landscape",
            "period": "2026-05",
            "displayed_brand_count": 5,
            "total_brands_in_market": 555,
            "level_segments": [
                {
                    "rank": rank,
                    "brand": brand,
                    "name": brand,
                    "ms_recent_pct": share,
                    "value_억원": sales,
                    "value": sales * 100_000_000,
                }
                for rank, (brand, share, sales) in enumerate(_MEMBERS, start=1)
            ],
        },
    }


def test_live_subject_series_is_sales_evidence_not_market_size() -> None:
    facts = evidence_from_calls([_live_sales_call()], "")
    subject_sales = {
        (fact.period, fact.value)
        for fact in facts
        if fact.entity == "리바로" and fact.metric == "매출"
    }

    assert ("2025-08", "79.63억원") in subject_sales
    assert ("2026-05", "80.39억원") in subject_sales
    assert not any(
        fact.metric == "시장규모" and fact.value in {"79.63억원", "80.39억원"}
        for fact in facts
    )


def test_live_subject_and_competitor_sales_tuples_remain_distinct() -> None:
    facts = evidence_from_calls([_live_sales_call()], "")
    latest_sales = {
        (fact.entity, fact.metric, fact.period, fact.unit, fact.value)
        for fact in facts
        if fact.metric == "매출" and fact.value
    }

    assert ("리바로", "매출", "2026-05", "억원", "80.39억원") in latest_sales
    for brand, _share, sales in _MEMBERS:
        assert (brand, "매출", "2026-05", "억원", f"{sales:.2f}억원") in latest_sales


def test_live_sales_cells_survive_claim_binding() -> None:
    answer = """### 지표
| 지표 | 수치(단위 포함) |
| --- | --- |
| 기간 | 2026-05 |
| 매출 | 80.39억원 |

**리바로 매출 시계열**
| 기간 | 매출 |
| --- | --- |
| 2025-08 | 79.63억원 |
| 2026-05 | 80.39억원 |"""
    facts = evidence_from_calls([_live_sales_call()], answer)

    result = verify_claim_bindings(
        question="리바로 매출 알려줘",
        answer=answer,
        facts=facts,
        expected_entities=("리바로",),
    )

    assert "근거 불일치로 제외" not in result.answer
    assert "80.39억원" in result.answer
    assert "79.63억원" in result.answer
    assert "METRIC_MISMATCH" not in result.blocked_reasons


def test_unsupported_subject_sales_remain_fail_closed() -> None:
    answer = """### 지표
| 지표 | 수치(단위 포함) |
| --- | --- |
| 매출 | 999.99억원 |"""
    facts = evidence_from_calls([_live_sales_call()], answer)

    result = verify_claim_bindings(
        question="리바로 매출 알려줘",
        answer=answer,
        facts=facts,
        expected_entities=("리바로",),
    )

    assert "수치를 제공하지 않습니다" in result.answer
    assert result.blocked_claim_count == 1
    assert result.blocked_numbers == ("999.99억원",)


def test_live_member_rows_emit_rank_facts_without_losing_display_count() -> None:
    facts = evidence_from_calls([_live_member_call()], "")
    ranks = {
        (fact.entity, fact.metric, fact.value, fact.path)
        for fact in facts
        if fact.metric == "순위"
    }

    assert ranks == {
        ("ml_555", "순위", "1", "render_data.level_segments[0].rank"),
        ("ml_555", "순위", "2", "render_data.level_segments[1].rank"),
        ("ml_555", "순위", "3", "render_data.level_segments[2].rank"),
        ("ml_555", "순위", "4", "render_data.level_segments[3].rank"),
        ("ml_555", "순위", "5", "render_data.level_segments[4].rank"),
    }
    assert any(fact.metric == "표시 브랜드 수" and fact.value == "5" for fact in facts)


def test_live_fifth_rank_survives_displayed_count_collision() -> None:
    answer = """## 해석

- — 시장의 구성 브랜드를 전략 mart에서 조회했습니다. 총 555개 중 5개 표시

### 시장 구성
| 항목 | 내용 |
| --- | --- |
| 시장 | — |
| 기준기간 | 2026-05 |
| 표시 범위 | 총 555개 중 5개 표시 |

### 구성 브랜드
| 순위 | 브랜드 |
| --- | --- |
| 1 | 로수젯 |
| 2 | 리피토 |
| 3 | 리바로젯 |
| 4 | 아토젯 |
| 5 | 로수바미브 |"""
    result = {
        "tool_calls": [_live_member_call()],
        "markdown_response": {"data_md": answer},
        "router_diagnostics": {
            "routing_v4": {
                "proposed_routing_signature": {
                    "proposed_calls": [
                        {"normalized_args": {"market_id": "ml_555"}},
                    ],
                },
            },
        },
        "resolution": {"market_id": "ml_555"},
    }

    revised = _apply_evidence_binding_gate(
        "고지혈증 시장 상위 5개",
        answer,
        result,
    )

    assert "| 5 | 로수바미브 |" in revised
    assert "| 근거 불일치로 제외 | 로수바미브 |" not in revised
    assert "METRIC_MISMATCH" not in result["_qa_claim_gate"]["blocked_reasons"]


def test_legacy_market_only_series_still_provides_market_size() -> None:
    facts = evidence_from_calls(
        [
            {
                "source": "UBIST",
                "tool": "get_market_landscape",
                "render_data": {
                    "market": "ml_555",
                    "view_type": "market_landscape",
                    "series": {
                        "2026-04": 205_000_000_000.0,
                        "2026-05": 213_925_043_300.0,
                    },
                },
            }
        ],
        "",
    )

    assert any(fact.metric == "시장규모" and fact.value == "2,139.25억원" for fact in facts)
