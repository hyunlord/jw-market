from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from jw_chat_agent_poc.orchestrator.comparison_compatibility import (
    incompatible_direct_comparison,
)
from jw_chat_agent_poc.service.answer_safety import (
    enforce_relational_numeric_claims_with_trace,
)
from jw_chat_agent_poc.service.app import compute_final_answer


MIXED_COMPARISON_ANSWER = """## 브랜드 매출 비교
| 브랜드 | 시작 기간 | 시작 매출 | 최신 기간 | 최신 매출 |
| --- | --- | --- | --- | --- |
| 가드렛 | 2025-08 | 1.90억원 | 2026-05 | 1.76억원 |
| 자누비아 | 2023-Q4 | 53.88억원 | 2026-Q1 | 34.19억원 |
| 트라젠타 | 2025-08 | 23.39억원 | 2026-05 | 23.32억원 |

## 브랜드 점유율 비교
| 브랜드 | 시작 점유율 | 최신 점유율 |
| --- | --- | --- |
| 가드렛 | 0.15% | 0.14% |
| 자누비아 | 1.73% | 0.52% |
| 트라젠타 | 1.88% | 1.79% |

가드렛은 하락, 자누비아는 하락, 트라젠타는 하락했습니다."""


def test_mixed_source_direct_comparison_uses_typed_guidance() -> None:
    result = enforce_relational_numeric_claims_with_trace(
        "가드렛, 자누비아, 트라젠타 매출과 점유율을 각각 알려줘",
        MIXED_COMPARISON_ANSWER,
        [
            _metric_call("가드렛", "UBIST", "2025-08", "2026-05", 976),
            deepcopy(_metric_call("가드렛", "UBIST", "2025-08", "2026-05", 976)),
            _metric_call("자누비아", "IQVIA NSA", "2023-Q4", "2026-Q1", 1055),
            _metric_call("트라젠타", "UBIST", "2025-08", "2026-05", 976),
        ],
    )

    assert result.disposition == "partial"
    assert result.failure_kind == "incompatible_comparison"
    assert "직접 비교할 수 없습니다" in result.answer
    assert "원천" in result.answer
    assert "기준기간" in result.answer
    assert "가드렛은 하락" not in result.answer


def test_same_source_direct_comparison_is_byte_identical() -> None:
    calls = [
        _metric_call("가드렛", "UBIST", "2025-08", "2026-05", 976),
        _metric_call("트라젠타", "UBIST", "2025-08", "2026-05", 976),
    ]
    answer = MIXED_COMPARISON_ANSWER.replace(
        "| 자누비아 | 2023-Q4 | 53.88억원 | 2026-Q1 | 34.19억원 |\n",
        "",
    ).replace(
        "| 자누비아 | 1.73% | 0.52% |\n",
        "",
    ).replace(
        "가드렛은 하락, 자누비아는 하락, 트라젠타는 하락했습니다.",
        "가드렛은 하락, 트라젠타는 하락했습니다.",
    )

    result = enforce_relational_numeric_claims_with_trace(
        "가드렛과 트라젠타 매출과 점유율을 비교해줘",
        answer,
        calls,
    )

    assert result.answer == answer
    assert result.failure_kind is None


def test_source_separated_listing_is_not_treated_as_direct_comparison() -> None:
    answer = (
        "## 원천별 확인 결과\n\n"
        "UBIST 월간 결과: 가드렛 1.76억원\n\n"
        "IQVIA NSA 분기 결과: 자누비아 34.19억원\n\n"
        "두 결과는 기준이 달라 직접 비교하지 않습니다."
    )

    result = enforce_relational_numeric_claims_with_trace(
        "가드렛과 자누비아를 원천별로 각각 알려줘",
        answer,
        [
            _metric_call("가드렛", "UBIST", "2025-08", "2026-05", 976),
            _metric_call("자누비아", "IQVIA NSA", "2023-Q4", "2026-Q1", 1055),
        ],
    )

    assert result.answer == answer
    assert result.failure_kind is None


def test_multi_source_guidance_is_idempotent_across_final_gate_reentry() -> None:
    calls = [
        _metric_call("가드렛", "UBIST", "2025-08", "2026-05", 976),
        _metric_call("자누비아", "IQVIA NSA", "2023-Q4", "2026-Q1", 1055),
    ]
    first = enforce_relational_numeric_claims_with_trace(
        "가드렛과 자누비아 매출과 점유율을 각각 알려줘",
        MIXED_COMPARISON_ANSWER,
        calls,
    )
    second = enforce_relational_numeric_claims_with_trace(
        "가드렛과 자누비아 매출과 점유율을 각각 알려줘",
        first.answer,
        calls,
    )

    assert second.answer == first.answer


def test_multi_source_incompatibility_takes_priority_over_cached_disclosure() -> None:
    cached = _metric_call("가드렛", "UBIST", "2025-08", "2026-05", 976)
    cached["cache_hit"] = True
    cached["data_as_of"] = "2026-05"
    result = enforce_relational_numeric_claims_with_trace(
        "가드렛과 자누비아 매출과 점유율을 각각 알려줘",
        MIXED_COMPARISON_ANSWER,
        [
            cached,
            _metric_call("자누비아", "IQVIA NSA", "2023-Q4", "2026-Q1", 1055),
            {"tool": "query_failed", "status": "error", "render_data": {"status": "error"}},
        ],
    )

    assert result.disposition == "partial"
    assert result.failure_kind == "incompatible_comparison"
    assert "직접 비교할 수 없습니다" in result.answer
    assert "기준 저장 결과" not in result.answer


def test_metric_split_calls_from_same_source_are_byte_identical() -> None:
    calls = [
        _only_metric(
            _metric_call("가드렛", "UBIST", "2025-08", "2026-05", 976),
            "sales",
        ),
        _only_metric(
            _metric_call("가드렛", "UBIST", "2025-08", "2026-05", 976),
            "share",
        ),
        _only_metric(
            _metric_call("트라젠타", "UBIST", "2025-08", "2026-05", 976),
            "sales",
        ),
        _only_metric(
            _metric_call("트라젠타", "UBIST", "2025-08", "2026-05", 976),
            "share",
        ),
    ]
    answer = MIXED_COMPARISON_ANSWER.replace(
        "| 자누비아 | 2023-Q4 | 53.88억원 | 2026-Q1 | 34.19억원 |\n",
        "",
    ).replace(
        "| 자누비아 | 1.73% | 0.52% |\n",
        "",
    ).replace(
        "가드렛은 하락, 자누비아는 하락, 트라젠타는 하락했습니다.",
        "가드렛은 하락, 트라젠타는 하락했습니다.",
    )

    result = enforce_relational_numeric_claims_with_trace(
        "가드렛과 트라젠타 매출과 점유율을 비교해줘",
        answer,
        calls,
    )

    assert result.answer == answer
    assert result.failure_kind is None


def test_single_point_mixed_source_comparison_is_incompatible() -> None:
    first = _metric_call("가드렛", "UBIST", "2026-05", "2026-05", 976)
    second = _metric_call("자누비아", "IQVIA NSA", "2026-Q1", "2026-Q1", 1055)
    for call in (first, second):
        call["render_data"]["brand_value_series_10pt"] = [
            call["render_data"]["brand_value_series_10pt"][-1],
        ]

    decision = incompatible_direct_comparison(
        "가드렛과 자누비아 매출을 비교해줘",
        "## 브랜드 매출 비교\n\n| 브랜드 | 매출 |\n| --- | --- |\n"
        "| 가드렛 | 1.76억원 |\n| 자누비아 | 34.19억원 |",
        [first, second],
    )

    assert decision is not None
    assert "source" in decision.mismatch_axes
    assert "grain" in decision.mismatch_axes
    assert "period" in decision.mismatch_axes


def test_compute_final_answer_preserves_multi_source_typed_state() -> None:
    final = compute_final_answer(
        "가드렛, 자누비아, 트라젠타 매출과 점유율을 각각 알려줘",
        {
            "general_view_ready": True,
            "answer": MIXED_COMPARISON_ANSWER,
            "tool_calls": [
                _metric_call("가드렛", "UBIST", "2025-08", "2026-05", 976),
                deepcopy(_metric_call("가드렛", "UBIST", "2025-08", "2026-05", 976)),
                _metric_call("자누비아", "IQVIA NSA", "2023-Q4", "2026-Q1", 1055),
                _metric_call("트라젠타", "UBIST", "2025-08", "2026-05", 976),
            ],
            "sources": ["UBIST", "IQVIA NSA"],
        },
        "multi-source-compatibility-final",
    )

    assert "직접 비교할 수 없습니다" in final.text
    assert "가드렛은 하락" not in final.text
    assert final.trace["qa_trace"]["final"]["failure_kind"] == "incompatible_comparison"
    assert "incompatible_comparison" in final.trace["qa_trace"]["claims"]["blocked_reasons"]


def test_compute_final_answer_preserves_partial_after_cached_first_pass() -> None:
    cached = _metric_call("가드렛", "UBIST", "2025-08", "2026-05", 976)
    cached["cache_hit"] = True
    cached["data_as_of"] = "2026-05"

    final = compute_final_answer(
        "가드렛과 자누비아 매출을 비교해줘",
        {
            "general_view_ready": True,
            "answer": "두 브랜드의 조회 결과를 정리합니다.",
            "tool_calls": [
                cached,
                _metric_call("자누비아", "IQVIA NSA", "2023-Q4", "2026-Q1", 1055),
            ],
            "sources": ["UBIST", "IQVIA NSA"],
        },
        "multi-source-cached-final",
    )

    assert "직접 비교할 수 없습니다" in final.text
    assert final.trace["qa_trace"]["final"]["disposition"] == "partial"
    assert final.trace["qa_trace"]["final"]["failure_kind"] == "incompatible_comparison"


def test_existing_market_mismatch_guidance_is_byte_identical() -> None:
    expected = (
        "리바로와 가드렛은 동일한 시장 정의와 분모에서 조회되지 않아 "
        "점유율 변화를 직접 비교할 수 없습니다.\n\n"
        "상태: 부분 확인\n\n"
        "확인된 범위: 개별 브랜드 지표 조회는 성공했지만 서로 다른 시장 기준의 "
        "수치를 한 표에서 직접 비교하지 않았습니다.\n\n"
        "대안: 각 브랜드의 시장 기준을 분리해 개별 추이를 확인해 주세요."
    )
    calls = [
        {
            "tool": "get_brand_metric",
            "status": "ok",
            "render_data": {
                "status": "ok",
                "brand": "리바로",
                "metric": "market_share",
                "brand_value_series_10pt": [
                    {"period": "2026-03", "ms_pct": 11.0},
                    {"period": "2026-04", "ms_pct": 12.0},
                ],
            },
        },
        {
            "tool": "query_failed",
            "status": "error",
            "render_data": {
                "status": "error",
                "reason_code": "incompatible_comparison",
                "anchor_brand": "리바로",
                "comparison_brand": "가드렛",
            },
        },
    ]

    result = enforce_relational_numeric_claims_with_trace(
        "리바로와 가드렛의 점유율 변화 비교",
        "리바로의 점유율은 상승했습니다.",
        calls,
    )

    assert result.answer == expected


@pytest.mark.parametrize(
    ("mutation", "expected_axes"),
    (
        ("source", ("source",)),
        ("grain", ("grain", "period")),
        ("period", ("period",)),
        ("metric", ("metric",)),
        ("unit", ("unit",)),
        ("market_definition", ("market_definition",)),
        ("denominator", ("denominator",)),
    ),
)
def test_each_compatibility_axis_is_compared(
    mutation: str,
    expected_axes: tuple[str, ...],
) -> None:
    first = _metric_call("가드렛", "UBIST", "2025-08", "2026-05", 976)
    second = _metric_call("트라젠타", "UBIST", "2025-08", "2026-05", 976)
    data = second["render_data"]
    if mutation == "source":
        second["source"] = "IQVIA NSA"
        data["source_label"] = "IQVIA NSA"
    elif mutation == "grain":
        data["brand_value_series_10pt"][0]["period"] = "2025-Q4"
        data["brand_value_series_10pt"][1]["period"] = "2026-Q1"
    elif mutation == "period":
        data["brand_value_series_10pt"][0]["period"] = "2025-09"
    elif mutation == "metric":
        for point in data["brand_value_series_10pt"]:
            point.pop("ms_pct")
    elif mutation == "unit":
        data["sales_unit"] = "USD"
    elif mutation == "market_definition":
        data["market_definition"] = "별도 시장"
    elif mutation == "denominator":
        data["total_brands_in_market"] = 1055

    decision = incompatible_direct_comparison(
        "가드렛과 트라젠타 매출과 점유율을 비교해줘",
        MIXED_COMPARISON_ANSWER,
        [first, second],
    )

    assert decision is not None
    assert decision.mismatch_axes == expected_axes


def test_missing_one_compatibility_axis_fails_closed() -> None:
    first = _metric_call("가드렛", "UBIST", "2025-08", "2026-05", 976)
    second = deepcopy(_metric_call("트라젠타", "UBIST", "2025-08", "2026-05", 976))
    second["render_data"].pop("market_definition")

    decision = incompatible_direct_comparison(
        "가드렛과 트라젠타 매출과 점유율을 비교해줘",
        MIXED_COMPARISON_ANSWER,
        [first, second],
    )

    assert decision is not None
    assert decision.mismatch_axes == ("market_definition",)


def test_conflicting_duplicate_brand_calls_do_not_skip_the_gate() -> None:
    first = _metric_call("가드렛", "UBIST", "2025-08", "2026-05", 976)
    conflicting = deepcopy(first)
    conflicting["render_data"]["brand_value_series_10pt"][0]["period"] = "2025-09"
    other = _metric_call("트라젠타", "UBIST", "2025-08", "2026-05", 976)

    decision = incompatible_direct_comparison(
        "가드렛과 트라젠타 매출과 점유율을 비교해줘",
        MIXED_COMPARISON_ANSWER,
        [first, conflicting, other],
    )

    assert decision is not None
    assert decision.mismatch_axes == ("period",)


def test_rank_comparison_heading_is_in_scope() -> None:
    first = _metric_call("가드렛", "UBIST", "2025-08", "2026-05", 976)
    second = _metric_call("자누비아", "IQVIA NSA", "2023-Q4", "2026-Q1", 1055)
    for call in (first, second):
        for rank, point in enumerate(call["render_data"]["brand_value_series_10pt"], start=1):
            point["rank"] = rank

    decision = incompatible_direct_comparison(
        "가드렛과 자누비아 순위를 비교해줘",
        "## 브랜드 순위 비교\n\n| 브랜드 | 순위 |\n| --- | --- |\n| 가드렛 | 1위 |\n| 자누비아 | 2위 |",
        [first, second],
    )

    assert decision is not None
    assert "source" in decision.mismatch_axes


def test_generic_brand_comparison_heading_is_in_scope() -> None:
    decision = incompatible_direct_comparison(
        "가드렛과 자누비아 매출을 비교해줘",
        "## 브랜드 비교\n\n| 브랜드 | 매출 |\n| --- | --- |\n"
        "| 가드렛 | 1.76억원 |\n| 자누비아 | 34.19억원 |",
        [
            _metric_call("가드렛", "UBIST", "2026-05", "2026-05", 976),
            _metric_call("자누비아", "IQVIA NSA", "2026-Q1", "2026-Q1", 1055),
        ],
    )

    assert decision is not None
    assert "source" in decision.mismatch_axes


def test_sales_scale_units_are_not_collapsed_to_currency_only() -> None:
    first = _metric_call("가드렛", "UBIST", "2025-08", "2026-05", 976)
    second = _metric_call("트라젠타", "UBIST", "2025-08", "2026-05", 976)
    first["render_data"]["sales_unit"] = "억원"
    second["render_data"]["sales_unit"] = "백만원"

    decision = incompatible_direct_comparison(
        "가드렛과 트라젠타 매출을 비교해줘",
        MIXED_COMPARISON_ANSWER,
        [first, second],
    )

    assert decision is not None
    assert decision.mismatch_axes == ("unit",)


def test_unrequested_metric_call_does_not_create_a_false_brand_mismatch() -> None:
    unrelated = _metric_call("리바로", "UBIST", "2025-08", "2026-05", 976)
    for point in unrelated["render_data"]["brand_value_series_10pt"]:
        point.pop("value_krw")
        point.pop("ms_pct")
        point["prescription_count"] = 10
    calls = [
        _only_metric(
            _metric_call("가드렛", "UBIST", "2025-08", "2026-05", 976),
            "sales",
        ),
        _only_metric(
            _metric_call("트라젠타", "UBIST", "2025-08", "2026-05", 976),
            "sales",
        ),
        unrelated,
    ]

    decision = incompatible_direct_comparison(
        "가드렛과 트라젠타 매출을 비교해줘",
        MIXED_COMPARISON_ANSWER,
        calls,
    )

    assert decision is None


def test_requested_metric_call_for_brand_absent_from_table_is_ignored() -> None:
    unrelated = _metric_call("리바로", "IQVIA NSA", "2023-Q4", "2026-Q1", 1055)
    calls = [
        _metric_call("가드렛", "UBIST", "2025-08", "2026-05", 976),
        _metric_call("트라젠타", "UBIST", "2025-08", "2026-05", 976),
        unrelated,
    ]
    answer = MIXED_COMPARISON_ANSWER.replace(
        "| 자누비아 | 2023-Q4 | 53.88억원 | 2026-Q1 | 34.19억원 |\n",
        "",
    ).replace(
        "| 자누비아 | 1.73% | 0.52% |\n",
        "",
    ).replace(
        "가드렛은 하락, 자누비아는 하락, 트라젠타는 하락했습니다.",
        "가드렛은 하락, 트라젠타는 하락했습니다.",
    )

    decision = incompatible_direct_comparison(
        "가드렛과 트라젠타 매출과 점유율을 비교해줘",
        answer,
        calls,
    )

    assert decision is None


def test_direct_intent_is_typed_before_comparison_heading_is_rendered() -> None:
    cached = _metric_call("가드렛", "UBIST", "2025-08", "2026-05", 976)
    cached["cache_hit"] = True
    cached["data_as_of"] = "2026-05"

    result = enforce_relational_numeric_claims_with_trace(
        "가드렛과 자누비아 매출을 비교해줘",
        "두 브랜드의 조회 결과를 정리합니다.",
        [
            cached,
            _metric_call("자누비아", "IQVIA NSA", "2023-Q4", "2026-Q1", 1055),
        ],
    )

    assert result.disposition == "partial"
    assert result.failure_kind == "incompatible_comparison"
    assert "직접 비교할 수 없습니다" in result.answer


def test_brand_name_contained_in_table_brand_is_not_selected() -> None:
    calls = [
        _metric_call("리바로젯", "UBIST", "2025-08", "2026-05", 976),
        _metric_call("로수젯", "UBIST", "2025-08", "2026-05", 976),
        _metric_call("리바로", "IQVIA NSA", "2023-Q4", "2026-Q1", 1055),
    ]

    decision = incompatible_direct_comparison(
        "리바로젯과 로수젯 매출을 비교해줘",
        "## 브랜드 비교\n\n| 브랜드 | 매출 |\n| --- | --- |\n"
        "| 리바로젯 | 10억원 |\n| 로수젯 | 20억원 |",
        calls,
    )

    assert decision is None


def test_two_explicit_prefix_related_brands_are_both_selected() -> None:
    decision = incompatible_direct_comparison(
        "리바로와 리바로젯 매출 비교",
        "두 브랜드의 조회 결과를 정리합니다.",
        [
            _metric_call("리바로", "IQVIA NSA", "2023-Q4", "2026-Q1", 1055),
            _metric_call("리바로젯", "UBIST", "2025-08", "2026-05", 976),
        ],
    )

    assert decision is not None
    assert decision.brands == ("리바로", "리바로젯")
    assert "source" in decision.mismatch_axes


def test_attached_hago_brand_separator_is_in_scope() -> None:
    decision = incompatible_direct_comparison(
        "가드렛하고 자누비아 매출 비교",
        "두 브랜드의 조회 결과를 정리합니다.",
        [
            _metric_call("가드렛", "UBIST", "2025-08", "2026-05", 976),
            _metric_call("자누비아", "IQVIA NSA", "2023-Q4", "2026-Q1", 1055),
        ],
    )

    assert decision is not None
    assert decision.brands == ("가드렛", "자누비아")


@pytest.mark.parametrize("separator", ("vs", "대비"))
def test_pre_heading_comparison_synonyms_are_in_scope(separator: str) -> None:
    result = enforce_relational_numeric_claims_with_trace(
        f"가드렛 {separator} 자누비아 매출",
        "두 브랜드의 조회 결과를 정리합니다.",
        [
            _metric_call("가드렛", "UBIST", "2025-08", "2026-05", 976),
            _metric_call("자누비아", "IQVIA NSA", "2023-Q4", "2026-Q1", 1055),
        ],
    )

    assert result.disposition == "partial"
    assert result.failure_kind == "incompatible_comparison"


def test_metadata_free_legacy_fixture_remains_not_applicable() -> None:
    first = _metric_call("가드렛", "", "2025-08", "2026-05", 976)
    second = _metric_call("트라젠타", "", "2025-08", "2026-05", 976)
    for call in (first, second):
        call["render_data"].pop("source_label")
        call["render_data"].pop("market_definition")
        call["render_data"].pop("total_brands_in_market")

    decision = incompatible_direct_comparison(
        "가드렛과 트라젠타 매출과 점유율을 비교해줘",
        MIXED_COMPARISON_ANSWER,
        [first, second],
    )

    assert decision is None


def _metric_call(
    brand: str,
    source: str,
    first_period: str,
    latest_period: str,
    denominator: int,
) -> dict[str, Any]:
    return {
        "tool": "get_brand_metric",
        "source": source,
        "status": "ok",
        "render_data": {
            "status": "ok",
            "source_label": source,
            "brand": brand,
            "market_definition": "가드렛 가드메트",
            "total_brands_in_market": denominator,
            "brand_value_series_10pt": [
                {"period": first_period, "value_krw": 100.0, "ms_pct": 2.0},
                {"period": latest_period, "value_krw": 90.0, "ms_pct": 1.0},
            ],
        },
    }


def _only_metric(call: dict[str, Any], metric: str) -> dict[str, Any]:
    for point in call["render_data"]["brand_value_series_10pt"]:
        if metric == "sales":
            point.pop("ms_pct")
        else:
            point.pop("value_krw")
    return call
