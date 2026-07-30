from __future__ import annotations

import pytest

from jw_chat_agent_poc.orchestrator.agent import _query_failed_metric_call
from jw_chat_agent_poc.orchestrator.answer_completeness import deterministic_top_n_share_answer
from jw_chat_agent_poc.orchestrator.market_answer_contract import enforce_market_answer_contract
from jw_chat_agent_poc.orchestrator.markdown_response import MarkdownResponseBuilder
from jw_chat_agent_poc.service.runtime_provenance import _ungrounded_numbers


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
| UBIST | 2026-05 | 전략뷰 | 스타틴 시장 | 555 | 전체 | % |
"""


def _top_call() -> dict:
    rows = (
        (1, "로수젯", 9.1264939920, 19_523_856_225.95),
        (2, "리피토", 6.1277726065, 13_108_840_203.03),
        (3, "리바로젯", 5.1167179108, 10_945_941_007.16),
        (4, "아토젯", 4.9487627406, 10_586_642_836.56),
        (5, "로수바미브", 4.1960520158, 8_976_406_092.54),
    )
    return {
        "tool": "get_brand_metric",
        "source": "UBIST",
        "render_data": {
            "status": "ok",
            "metric": "market_top_brands",
            "period": "2026-05",
            "market_id": "ml_006",
            "market_name": "스타틴 시장",
            "total_brands_in_market": 555,
            "level_segments": [
                {
                    "rank": rank,
                    "brand": brand,
                    "ms_recent_pct": share,
                    "value": sales,
                    "value_억원": sales / 100_000_000,
                }
                for rank, brand, share, sales in rows
            ],
        },
    }


def test_top5_sum_uses_raw_shares_before_final_rounding() -> None:
    answer = deterministic_top_n_share_answer(
        "리바로 시장 상위 5개 브랜드 점유율과 합계를 알려줘",
        TOP_FACT,
        [_top_call()],
    )

    assert answer.startswith("상위 5개 합계 시장점유율은 29.52%입니다.")
    assert "29.53%" not in answer


def test_concentration_requires_hhi_and_raw_cr5() -> None:
    answer = enforce_market_answer_contract(
        question="이 시장 집중도는 어때? HHI와 CR5를 알려줘",
        answer="HHI는 253.62입니다.",
        tool_calls=[_top_call(), {"tool": "get_brand_metric", "render_data": {"status": "ok", "metric": "hhi", "hhi": 253.62}}],
    )

    assert "HHI 253.62" in answer
    assert "CR5 29.52%" in answer


def test_supported_disease_market_uses_grounded_top_five_instead_of_status_rejection() -> None:
    answer = enforce_market_answer_contract(
        question="고지혈증 시장 상위 5개 브랜드와 HHI, CR5를 알려줘",
        answer="",
        tool_calls=[
            _top_call(),
            {"tool": "get_brand_metric", "render_data": {"status": "ok", "metric": "hhi", "hhi": 253.62}},
        ],
    )

    assert "지원되지 않는 시장" not in answer
    assert "HHI 253.62" in answer
    assert "CR5 29.52%" in answer
    assert "| 1위 | 로수젯 | 9.13% |" in answer
    assert "| 5위 | 로수바미브 | 4.20% |" in answer


def test_strategy_identifier_keeps_strategy_view_and_public_name() -> None:
    call = {
        "tool": "get_market_landscape",
        "source": "UBIST",
        "render_data": {
            "status": "ok",
            "metric": "market_size",
            "market_id": "ml_006",
            "market_name": "리바로·리바로젯 시장",
            "view_type": "market_landscape",
            "period": "2025-04",
            "market_size_억원": 2106.71557456,
        },
    }

    answer = enforce_market_answer_contract(
        question="ml_006 2025-04 시장규모",
        answer="일반뷰 후보를 찾지 못했습니다.",
        tool_calls=[call],
    )

    assert "전략뷰" in answer
    assert "2,106.715575억원" in answer
    assert "ml_006" not in answer
    assert "market_landscape" not in answer


def test_strategy_market_size_precision_is_grounded_by_raw_market_fact() -> None:
    call = {
        "tool": "get_market_landscape",
        "source": "UBIST",
        "render_data": {
            "status": "ok",
            "metric": "market_size",
            "market_id": "ml_006",
            "market_name": "리바로·리바로젯 시장",
            "view_type": "market_landscape",
            "period": "2025-04",
            "market_size_억원": 2106.71557456,
        },
    }
    question = "ml_006 2025-04 시장규모"
    answer = enforce_market_answer_contract(question=question, answer="", tool_calls=[call])
    response = MarkdownResponseBuilder().build(brand="", calls=[call], sources=["UBIST"]).to_dict()

    assert "2,106.715575억원" in answer
    assert _ungrounded_numbers(answer, response) == ()


def test_strategy_market_size_golden_postcheck_blocks_wrong_value() -> None:
    call = {
        "tool": "get_market_landscape",
        "source": "UBIST",
        "render_data": {
            "status": "ok",
            "metric": "market_size",
            "market_id": "ml_006",
            "view_type": "market_landscape",
            "period": "2025-04",
            "market_size_억원": 2139.250433,
            "total_brands_in_market": 470,
        },
    }

    answer = enforce_market_answer_contract(
        question="ml_006 2025-04 시장규모",
        answer="2,139.250433억원입니다.",
        tool_calls=[call],
    )

    assert answer == "승인된 2025-04 전략 시장 기준값과 일치하지 않아 수치를 표시하지 않습니다."
    assert "2,139" not in answer


def test_strategy_market_size_golden_postcheck_blocks_inapplicable_denominator() -> None:
    for denominator in (470, 555):
        call = {
            "tool": "get_market_landscape",
            "source": "UBIST",
            "render_data": {
                "status": "ok",
                "metric": "market_size",
                "market_id": "ml_006",
                "view_type": "market_landscape",
                "period": "2025-04",
                "market_size_억원": 2106.71557456,
                "total_brands_in_market": denominator,
            },
        }

        answer = enforce_market_answer_contract(
            question="ml_006 2025-04 시장규모",
            answer="2,106.715575억원입니다.",
            tool_calls=[call],
        )

        assert answer == "승인된 2025-04 전략 시장 기준값과 일치하지 않아 수치를 표시하지 않습니다."


def test_strategy_market_size_golden_postcheck_ignores_unrelated_market_calls() -> None:
    unrelated = {
        "tool": "get_market_landscape",
        "source": "UBIST",
        "render_data": {
            "status": "ok",
            "metric": "market_size",
            "market_id": "ml_999",
            "view_type": "market_landscape",
            "period": "2025-04",
            "market_size_억원": 999.0,
            "total_brands_in_market": 1,
        },
    }
    approved = {
        "tool": "get_market_landscape",
        "source": "UBIST",
        "render_data": {
            "status": "ok",
            "metric": "market_size",
            "market_id": "ml_006",
            "view_type": "market_landscape",
            "period": "2025-04",
            "market_size_억원": 2106.71557456,
        },
    }

    answer = enforce_market_answer_contract(
        question="ml_006 2025-04 시장규모",
        answer="",
        tool_calls=[unrelated, approved],
    )

    assert "2,106.715575억원" in answer
    assert "일치하지 않아" not in answer


def test_period_free_strategy_market_size_keeps_latest_mart_value() -> None:
    call = {
        "tool": "get_market_landscape",
        "source": "UBIST",
        "render_data": {
            "status": "ok",
            "metric": "market_size",
            "market_id": "ml_006",
            "view_type": "market_landscape",
            "period": "2026-05",
            "market_size_억원": 2139.250433,
            "total_brands_in_market": 555,
        },
    }

    answer = enforce_market_answer_contract(
        question="ml_006 시장 규모",
        answer="",
        tool_calls=[call],
    )

    assert "2026-05" in answer
    assert "2,139.250433억원" in answer


def test_unsupported_region_repurchase_never_falls_back_to_market_totals() -> None:
    answer = enforce_market_answer_contract(
        question="리바로 지역별 재구매율을 알려줘",
        answer="전체 시장 상위 브랜드는 로수젯이며 점유율은 9.13%입니다.",
        tool_calls=[_top_call()],
    )

    assert answer.startswith("현재 DB는 지역별 재구매율을 지원하지 않습니다.")
    assert "로수젯" not in answer
    assert "9.13%" not in answer


def test_missing_subject_is_not_reported_as_missing_data() -> None:
    answer = enforce_market_answer_contract(
        question="매출 알려줘",
        answer="매출 데이터가 확보되지 않았습니다.",
        tool_calls=[],
    )

    assert answer.startswith("브랜드·시장·기간을 지정해 주세요.")


def test_future_period_reports_owned_range_without_fake_range() -> None:
    answer = enforce_market_answer_contract(
        question="리바로 2030년 매출",
        answer="기준기간은 2026-05~2030이며 데이터가 없습니다.",
        tool_calls=[
            {
                "tool": "get_brand_metric",
                "source": "UBIST",
                "render_data": {
                    "status": "unsupported",
                    "brand": "리바로",
                    "requested_period": "2030",
                    "available_to": "2026-05",
                },
            }
        ],
    )

    assert "보유 데이터는 2026-05까지이며 2030년 실적은 없습니다." in answer
    assert "2026-05~2030" not in answer
    assert "| 보유 범위 | 2026-05 | 해당 없음 | 해당 없음 | 해당 없음 | 전체 | 억원 |" in answer
    assert "| UBIST |" not in answer


def test_unsupported_metric_provenance_does_not_reuse_market_axes() -> None:
    answer = enforce_market_answer_contract(
        question="리바로 지역별 재구매율을 알려줘",
        answer="전체 시장 상위 브랜드는 로수젯이며 점유율은 9.13%입니다.",
        tool_calls=[_top_call()],
    )

    assert "| 지원 범위 | 해당 없음 | 해당 없음 | 해당 없음 | 해당 없음 | 전체 | % |" in answer
    assert "| UBIST |" not in answer
    assert "555" not in answer


def test_patent_provenance_uses_public_source_name() -> None:
    answer = enforce_market_answer_contract(
        question="리바로 특허 만료일을 알려줘",
        answer="리바로의 국내 특허 만료일은 2027-01-15입니다.",
        tool_calls=[
            {
                "tool": "get_patent_expiry",
                "source": "nedrug_mcp",
                "render_data": {
                    "status": "ok",
                    "source": "nedrug_mcp",
                    "period": "2027-01",
                },
            }
        ],
    )

    assert "nedrug_mcp" not in answer
    assert "| 식약처 의약품 특허 정보 |" in answer


def test_internal_identifiers_and_causal_assertion_are_removed() -> None:
    answer = enforce_market_answer_contract(
        question="왜 리바로 점유율이 하락했나?",
        answer=(
            "market_landscape의 query_spec과 nedrug_mcp를 보면 복합제 성장 압력 때문에 하락했습니다."
        ),
        tool_calls=[],
    )

    assert "market_landscape" not in answer
    assert "query_spec" not in answer
    assert "nedrug_mcp" not in answer
    assert "때문" not in answer
    assert "원인으로 확정할 수 없습니다" in answer
    assert "추가 확인" in answer


def test_provenance_is_rebuilt_with_seven_public_nonempty_fields() -> None:
    answer = enforce_market_answer_contract(
        question="리바로 상급종병 채널 매출",
        answer="상급종병 채널 매출입니다.",
        tool_calls=[
            {
                "tool": "get_brand_metric",
                "source": "UBIST",
                "applied_filters": {"channel": "상급종병"},
                "render_data": {
                    "status": "ok",
                    "brand": "리바로",
                    "metric": "sales",
                    "period": "2026-05",
                    "market_name": "리바로·리바로젯 시장",
                    "view_type": "market_landscape",
                    "rank_denominator": 555,
                    "sales_억원": 12.3,
                },
            }
        ],
    )

    assert "| 출처 | 기준기간 | 뷰 | 시장정의 | 분모 | 채널 | 단위 |" in answer
    assert "| UBIST | 2026-05 | 전략뷰 | 리바로·리바로젯 시장 | 555 | 상급종병 | 억원 |" in answer
    assert "—" not in answer


def test_market_size_provenance_uses_amount_unit_and_visible_market_definition() -> None:
    answer = enforce_market_answer_contract(
        question="C10A1 시장 규모를 알려줘",
        answer=(
            "## 일반뷰 (ATC4)\n\n"
            "- 시장: [C10A1] 스타틴류\n"
            "- 시장 규모 (2026-05): 870.2억원\n"
            "- Top 5: 리피토 (15.06%)\n\n"
            "점유율 분모: ATC4 C10A1 시장 전체 sales"
        ),
        tool_calls=[
            {
                "tool": "general_view_dynamic_market",
                "source": "UBIST",
                "render_data": {"period": "2026-05", "view_type": "general_view"},
            }
        ],
    )

    assert "| UBIST | 2026-05 | 일반뷰 | [C10A1] 스타틴류 | ATC4 C10A1 시장 전체 sales | 전체 | 억원 |" in answer


def test_trend_question_surfaces_full_monthly_series_instead_of_average() -> None:
    answer = enforce_market_answer_contract(
        question="리바로 최근 6개월 점유율 추이",
        answer="최근 6개월 평균은 3.81%입니다.",
        tool_calls=[
            {
                "tool": "get_brand_metric",
                "source": "UBIST",
                "render_data": {
                    "status": "ok",
                    "brand": "리바로",
                    "metric": "series",
                    "unit": "%",
                    "series": [
                        {"period": f"2026-{month:02d}", "ms_recent_pct": value}
                        for month, value in enumerate((3.72, 3.78, 3.81, 3.93, 3.84, 3.76), 1)
                    ],
                },
            }
        ],
    )

    assert len([line for line in answer.splitlines() if line.startswith("| 2026-") and line.count("|") == 3]) == 6
    assert "평균은 3.81%" not in answer


def test_sales_trend_selects_sales_values_instead_of_share_values() -> None:
    answer = enforce_market_answer_contract(
        question="리바로 최근 매출 추이",
        answer="최근 점유율 추이입니다.",
        tool_calls=[
            {
                "tool": "get_brand_metric",
                "source": "UBIST",
                "render_data": {
                    "status": "ok",
                    "brand": "리바로",
                    "unit": "%",
                    "brand_value_series_10pt": [
                        {
                            "period": f"2026-{month:02d}",
                            "ms_recent_pct": 3.7 + month / 100,
                            "value_억원": 70 + month,
                        }
                        for month in range(1, 7)
                    ],
                },
            }
        ],
    )

    assert "| 2026-01 | 71.00억원 |" in answer
    assert "3.71%" not in answer


def test_ambiguous_trend_preserves_share_unit_selected_from_series() -> None:
    answer = enforce_market_answer_contract(
        question="그중 1위 브랜드의 월별 추이는?",
        answer="월별 추이입니다.",
        tool_calls=[
            {
                "tool": "get_brand_metric",
                "source": "UBIST",
                "render_data": {
                    "status": "ok",
                    "brand": "로수젯",
                    "brand_value_series_10pt": [
                        {"period": f"2026-{month:02d}", "ms_recent_pct": 9 + month / 100}
                        for month in range(1, 7)
                    ],
                },
            }
        ],
    )

    assert "| 2026-01 | 9.01% |" in answer
    assert "9.01억원" not in answer


def test_historical_sales_uses_structured_value_without_llm_interpretation() -> None:
    answer = enforce_market_answer_contract(
        question="리바로 2025년 4월 매출",
        answer="시장 장악력에 따른 압박을 보여주는 83.18억원입니다.",
        tool_calls=[
            {
                "tool": "get_brand_metric",
                "source": "UBIST",
                "render_data": {
                    "status": "ok",
                    "brand": "리바로",
                    "period": "2025-04",
                    "sales_억원": 83.184115,
                    "market_name": "해당 전략 시장",
                    "view_type": "market_landscape",
                    "rank_denominator": 555,
                },
            }
        ],
    )

    assert answer.startswith("2025-04 리바로 매출은 83.184115억원입니다.")
    assert "장악력" not in answer
    assert "| 억원 |" in answer


def test_csd_trend_surfaces_every_month_and_csd_provenance() -> None:
    answer = enforce_market_answer_contract(
        question="리바로 영업활동 추이",
        answer="2025-06 1,775건에서 2026-05 1,769건입니다.",
        tool_calls=[
            {
                "tool": "csd_activity_trend",
                "source": "CSD ChannelDynamics",
                "render_data": {
                    "status": "ok",
                    "brand": "리바로",
                    "source_label": "CSD ChannelDynamics",
                    "series": [
                        {"period": "2025-06", "product_details": 1775},
                        {"period": "2025-07", "product_details": 1801},
                        {"period": "2026-05", "product_details": 1769},
                    ],
                },
            }
        ],
    )

    assert len([line for line in answer.splitlines() if line.startswith("| 20") and line.count("|") == 3]) == 3
    assert "| CSD ChannelDynamics |" in answer
    assert "| 건 |" in answer


def test_hira_trend_uses_every_requested_year_instead_of_latest_snapshot() -> None:
    calls = [
        {
            "tool": "hira_disease_hospitalization_outpatient_stats",
            "source": "hira_disease",
            "render_data": {
                "status": "live",
                "request": {"sickCd": "E78", "year": str(year)},
                "mapping_disease_name": "지질단백질대사장애 및 기타 지질증",
                "items": [
                    {"inpatOpat": "입원", "ptntCnt": 3_000 + year},
                    {"inpatOpat": "외래", "ptntCnt": count},
                ],
            },
        }
        for year, count in ((2020, 900_000), (2021, 980_000), (2022, 1_080_000), (2023, 1_190_000), (2024, 1_305_727))
    ]

    answer = enforce_market_answer_contract(
        question="고지혈증 환자수 추이를 알려줘 (HIRA)",
        answer="2024년 외래 환자수는 1,305,727명입니다.",
        tool_calls=calls,
    )

    assert len([line for line in answer.splitlines() if line.startswith("| 20") and line.count("|") == 3]) == 5
    assert "| 2020 | 900,000명 |" in answer
    assert "| 2024 | 1,305,727명 |" in answer
    assert "| 심사평가원(HIRA) 질병통계 |" in answer
    assert "| 2020~2024 |" in answer


def test_brand_comparison_deduplicates_calls_and_computes_share_direction() -> None:
    def call(brand: str, start: float, latest: float, start_sales: float, latest_sales: float) -> dict:
        return {
            "tool": "get_brand_metric",
            "source": "UBIST",
            "render_data": {
                "status": "ok",
                "brand": brand,
                "period": "2026-05",
                "market_name": "리바로·리바로젯 시장",
                "view_type": "market_landscape",
                "rank_denominator": 555,
                "brand_value_series_10pt": [
                    {"period": "2025-08", "value_krw": start_sales, "ms_pct": start},
                    {"period": "2026-05", "value_krw": latest_sales, "ms_pct": latest},
                ],
            },
        }

    livaro = call("리바로", 3.93, 3.76, 7_963_000_000, 8_039_000_000)
    rosuzet = call("로수젯", 9.10, 9.13, 18_459_000_000, 19_524_000_000)
    answer = enforce_market_answer_contract(
        question="리바로와 로수젯을 비교해줘",
        answer="리바로는 상승, 리바로는 상승, 로수젯은 상승했습니다.",
        tool_calls=[livaro, livaro, rosuzet, rosuzet, rosuzet],
    )

    # Count inside the comparison table only. The source table now carries a brand column, so
    # a bare answer-wide count would also match its cells.
    comparison = answer.partition("## 브랜드 비교")[2].partition("## ")[0]
    assert comparison.count("| 리바로 |") == 1
    assert comparison.count("| 로수젯 |") == 1
    assert "| 리바로 | 2025-08 3.93% | 2026-05 3.76% | 하락 |" in answer
    assert "| 로수젯 | 2025-08 9.10% | 2026-05 9.13% | 상승 |" in answer


def test_brand_rank_comparison_adds_latest_rank_column() -> None:
    # Given: the comparison tool returned a current rank for every requested brand.
    calls = [
        {
            "tool": "get_brand_metric",
            "source": "UBIST",
            "render_data": {
                "status": "ok",
                "brand": brand,
                "rank": rank,
                "brand_value_series_10pt": [
                    {"period": "2025-08", "value_krw": start_sales, "ms_pct": start_share},
                    {"period": "2026-05", "value_krw": latest_sales, "ms_pct": latest_share},
                ],
            },
        }
        for brand, rank, start_share, latest_share, start_sales, latest_sales in (
            ("리바로", 7, 3.93, 3.76, 7_963_000_000, 8_039_000_000),
            ("리바로젯", 4, 4.88, 5.12, 9_550_000_000, 10_946_000_000),
            ("로수젯", 1, 9.10, 9.13, 18_459_000_000, 19_524_000_000),
            ("리피토", 2, 6.31, 6.13, 13_752_000_000, 13_109_000_000),
        )
    ]

    # When: the answer contract renders an explicit multi-brand rank comparison.
    answer = enforce_market_answer_contract(
        question="리바로, 리바로젯, 로수젯, 리피토 네 브랜드 순위를 비교해줘",
        answer="",
        tool_calls=calls,
    )

    # Then: the existing comparison columns remain and current rank is appended.
    assert (
        "| 브랜드 | 시작 점유율 | 최신 점유율 | 방향 | 시작 매출 | 최신 매출 | 최신 순위 |"
        in answer
    )
    assert "| 리바로 | 2025-08 3.93% | 2026-05 3.76% | 하락 | 79.63억원 | 80.39억원 | 7위 |" in answer
    assert "| 리바로젯 | 2025-08 4.88% | 2026-05 5.12% | 상승 | 95.50억원 | 109.46억원 | 4위 |" in answer
    assert "| 로수젯 | 2025-08 9.10% | 2026-05 9.13% | 상승 | 184.59억원 | 195.24억원 | 1위 |" in answer
    assert "| 리피토 | 2025-08 6.31% | 2026-05 6.13% | 하락 | 137.52억원 | 131.09억원 | 2위 |" in answer


def test_channel_ranking_uses_only_channel_filtered_rows() -> None:
    answer = enforce_market_answer_contract(
        question="리바로 시장에서 상급종합병원 채널 내 상위 브랜드",
        answer="전체시장 1위 로수젯입니다.",
        tool_calls=[
            {
                "tool": "get_brand_metric",
                "source": "UBIST",
                "applied_filters": {"channel": "상급종합병원"},
                "render_data": {
                    "metric": "market_top_brands",
                    "period": "2026-05",
                    "level_segments": [
                        {"rank": 1, "brand": "채널브랜드A", "ms_recent_pct": 12.345},
                        {"rank": 2, "brand": "채널브랜드B", "ms_recent_pct": 10.111},
                    ],
                },
            },
            _top_call(),
        ],
    )

    assert "채널브랜드A" in answer
    assert "채널브랜드B" in answer
    assert "로수젯" not in answer


def test_specialty_answer_excludes_unfiltered_market_rows() -> None:
    answer = enforce_market_answer_contract(
        question="리바로 진료과별 분포를 알려줘",
        answer="전체시장 1위 로수젯과 진료과별 결과입니다.",
        tool_calls=[
            _top_call(),
            {
                "tool": "query_spec",
                "source": "UBIST",
                "render_data": {
                    "requested_dimension": "specialty",
                    "period": "2026-05",
                    "brand": "리바로",
                    "level_segments": [
                        {"rank": 1, "name": "순환기", "value_억원": 9.55, "ms_recent_pct": 2.90},
                        {"rank": 2, "name": "내분비", "value_억원": 9.54, "ms_recent_pct": 8.74},
                    ],
                },
            },
        ],
    )

    assert "## 진료과별 분포" in answer
    assert "순환기" in answer
    assert "내분비" in answer
    assert "로수젯" not in answer


def test_unavailable_states_are_not_conflated() -> None:
    mapping = enforce_market_answer_contract("고지혈증 시장 규모", "원천 없음", [])
    entity = enforce_market_answer_contract(
        "가상브랜드XYZ 매출",
        "가상브랜드XYZ의 매출 데이터는 현재 시스템에서 보유하고 있지 않아 확인이 불가능합니다.",
        [],
    )
    technical = enforce_market_answer_contract(
        "리바로 매출", "확인 불가", [{"tool": "get_brand_metric", "render_data": {"status": "query_failed"}}]
    )

    assert mapping.startswith("현재 지원되지 않는 시장 매핑입니다.")
    assert entity.startswith("브랜드 목록에서 일치 항목을 찾지 못했습니다.")
    assert technical.startswith("데이터 존재 여부를 확인하지 못했습니다. 조회 오류입니다.")


@pytest.mark.parametrize(
    "question",
    (
        "아일리아 경쟁 약물 현황 알려줘",
        "아일리아 쪽 경쟁 상황 한눈에 보여줘",
    ),
)
def test_market_unresolved_preserves_typed_brand_guidance(question: str) -> None:
    call = _query_failed_metric_call(
        "아일리아",
        "market_share",
        (),
        LookupError("strategic mart has no market for brand: 아일리아"),
    )

    answer = enforce_market_answer_contract(question, call["summary_text"], [call])

    assert answer.startswith("아일리아는 현재 전략 시장 분류에 연결되어 있지 않아")
    assert "경쟁 분석을 제공할 수 없습니다." in answer
    assert "조회 오류" not in answer


def test_unknown_query_failure_is_not_promoted_to_market_unresolved() -> None:
    call = _query_failed_metric_call(
        "가상브랜드XYZ",
        "market_share",
        (),
        LookupError("mart brand not found: brand=가상브랜드XYZ"),
    )

    answer = enforce_market_answer_contract("가상브랜드XYZ 경쟁 현황", call["summary_text"], [call])

    assert answer.startswith("데이터 존재 여부를 확인하지 못했습니다. 조회 오류입니다.")
    assert "전략 시장 분류" not in answer


def test_causal_question_separates_observation_from_unverified_hypothesis() -> None:
    answer = enforce_market_answer_contract(
        question="왜 리바로 점유율이 하락했나?",
        answer="복합제의 강력한 성장 압력 때문에 리바로 점유율이 하락했습니다.",
        tool_calls=[
            {
                "tool": "get_brand_metric",
                "source": "UBIST",
                "render_data": {
                    "brand": "리바로",
                    "period": "2026-05",
                    "brand_value_series_10pt": [
                        {"period": "2025-08", "ms_pct": 3.93},
                        {"period": "2026-05", "ms_pct": 3.76},
                    ],
                },
            }
        ],
    )

    assert "## 관찰" in answer
    assert "3.93%" in answer and "3.76%" in answer
    assert "## 가설과 한계" in answer
    assert "원인으로 확정할 수 없습니다" in answer
    assert "때문" not in answer


def test_internal_identifiers_leave_no_empty_user_facing_labels() -> None:
    answer = enforce_market_answer_contract(
        question="리바로 시장 규모",
        answer="## 전략뷰 (ml_006)\nmarket_landscape query_spec 결과입니다.",
        tool_calls=[],
    )

    assert "ml_006" not in answer
    assert "market_landscape" not in answer
    assert "query_spec" not in answer
    assert "전략뷰 ()" not in answer


def test_news_answer_drops_unsupported_causal_interpretation_but_keeps_news() -> None:
    answer = enforce_market_answer_contract(
        question="리바로 관련 최근 이슈 뭐 있어?",
        answer=(
            "- 뉴스: 신제품 출시 사실이 확인됐습니다.\n"
            "시장 중심이 복합제로 이동했기 때문에 경쟁 압력이 커졌습니다."
        ),
        tool_calls=[{"tool": "news_search", "source": "뉴스/이슈", "render_data": {"status": "ok"}}],
    )

    assert "신제품 출시 사실" in answer
    assert "시장 중심" not in answer
    assert "때문" not in answer


def test_news_answer_drops_possessive_market_center_causal_claim() -> None:
    answer = enforce_market_answer_contract(
        question="리바로 관련 최근 이슈를 알려줘",
        answer=(
            "- 뉴스: 리바로젯 매출 1위 기사가 확인됐습니다.\n"
            "리바로는 시장의 중심이 복합제로 이동함에 따라 점유율 압박을 받고 있습니다."
        ),
        tool_calls=[{"tool": "news_search", "source": "뉴스/이슈", "render_data": {"status": "ok"}}],
    )

    assert "리바로젯 매출 1위" in answer
    assert "시장의 중심" not in answer
    assert "점유율 압박" not in answer


def test_channel_distribution_provenance_names_the_axis_not_one_member() -> None:
    answer = enforce_market_answer_contract(
        question="리바로 채널별 매출을 상급종병, 종병, 병원, 의원으로 나눠 알려줘",
        answer="채널별 매출입니다.",
        tool_calls=[
            {
                "tool": "query_spec",
                "source": "UBIST",
                "render_data": {
                    "period": "2026-05",
                    "requested_dimension": "channel",
                    "level_segments": [{"name": "상급종병", "value_억원": 15.97}],
                },
            }
        ],
    )

    assert "| 채널별 | 억원 |" in answer
    assert "| 상급종병 | 억원 |" not in answer


def test_csd_provenance_excludes_unrelated_market_metric_call() -> None:
    answer = enforce_market_answer_contract(
        question="리바로 영업활동 추이를 알려줘",
        answer="영업활동 추이입니다.",
        tool_calls=[
            {
                "tool": "csd_activity_trend",
                "source": "CSD ChannelDynamics",
                "render_data": {
                    "brand": "리바로",
                    "series": [
                        {"period": "2026-03", "product_details": 3},
                        {"period": "2026-04", "product_details": 4},
                        {"period": "2026-05", "product_details": 5},
                    ],
                },
            },
            {
                "tool": "get_brand_metric",
                "source": "UBIST",
                "render_data": {"brand": "리바로", "period": "2026-05", "sales_억원": 80.39},
            },
        ],
    )

    assert "| CSD ChannelDynamics |" in answer
    assert "| UBIST |" not in answer


def test_unknown_brand_uses_entity_status_and_complete_public_provenance() -> None:
    answer = enforce_market_answer_contract(
        question="가상브랜드XYZ 매출 알려줘",
        answer=(
            "가상브랜드XYZ의 매출 데이터는 현재 시스템 내에 존재하지 않아 정보를 제공할 수 없습니다.\n\n"
            "## 출처\n| 출처 | 기준기간 | 뷰 | 시장정의 | 분모 | 채널 | 단위 |\n"
            "| --- | --- | --- | --- | --- | --- | --- |\n| — | — | — | — | — | 전체 | — |"
        ),
        tool_calls=[],
    )

    assert answer.startswith("브랜드 목록에서 일치 항목을 찾지 못했습니다.")
    assert "—" not in answer
    assert "| 브랜드 카탈로그 | 해당 없음 | 해당 없음 | 해당 없음 | 해당 없음 | 전체 | 억원 |" in answer


def test_derived_calculation_does_not_add_a_hollow_provenance_row() -> None:
    answer = enforce_market_answer_contract(
        question="리바로 시장 상위 5개 브랜드 점유율과 합계를 알려줘",
        answer="상위 5개 합계 시장점유율은 29.52%입니다.",
        tool_calls=[
            {
                "tool": "get_brand_metric",
                "source": "UBIST",
                "render_data": {
                    "period": "2026-05",
                    "view_type": "market_landscape",
                    "market_name": "리바로 전략 시장",
                },
            },
            {
                "tool": "agent_calculation",
                "source": "UBIST",
                "render_data": {"period": "2025-08~2026-05", "value": 29.515799},
            },
        ],
    )

    provenance_rows = [line for line in answer.splitlines() if line.startswith("| UBIST |")]
    assert provenance_rows == [
        "| UBIST | 2026-05 | 전략뷰 | 리바로 전략 시장 | 해당 없음 | 전체 | % | 해당 없음 |"
    ]


def test_quarter_metric_does_not_render_query_plan_as_a_source_row() -> None:
    answer = enforce_market_answer_contract(
        question="리바로 2025년 2분기 매출",
        answer="2025-Q2 리바로 매출은 242.72억원입니다.",
        tool_calls=[
            {
                "tool": "query_spec",
                "source": "UBIST",
                "render_data": {
                    "metric": "query_spec",
                    "period": "2026-05",
                    "query_spec": {"filters": {"brand": "리바로", "period": "2025-Q2"}},
                },
            },
            {
                "tool": "get_brand_metric",
                "source": "UBIST",
                "render_data": {
                    "brand": "리바로",
                    "metric": "sales",
                    "period": "2025-Q2",
                    "sales_억원": 242.72,
                    "sales_krw": 24_272_468_115.55,
                    "query_spec": {
                        "view": "market_landscape",
                        "filters": {"brand": "리바로", "period": "2025-Q2"},
                        "total_brands_in_market": 555,
                    },
                },
            },
        ],
    )

    provenance_rows = [line for line in answer.splitlines() if line.startswith("| UBIST |")]
    assert provenance_rows == [
        "| UBIST | 2025-Q2 | 전략뷰 | 요청 브랜드의 전략 시장 | 555 | 전체 | 억원 | 리바로 |"
    ]
    assert answer.startswith("2025-Q2 리바로 매출은 242.72억원입니다.")
    assert "2026-05" not in answer


def test_brand_market_size_uses_verified_scope_fact_instead_of_channel_dump() -> None:
    answer = enforce_market_answer_contract(
        question="리바로 시장 규모",
        answer="2026-05 기준 리바로 채널별 매출은 로수젯 195.24억원 순입니다.",
        tool_calls=[
            {
                "tool": "get_brand_metric",
                "source": "UBIST",
                "render_data": {
                    "brand": "리바로",
                    "metric": "market_top_brands",
                    "market_id": "ml_006",
                    "market_name": "ml_006",
                    "period": "2026-05",
                    "market_size_recent_krw": 213_925_043_319.36026,
                    "market_size_억원": 2_139.25,
                    "total_brands_in_market": 555,
                },
            }
        ],
    )

    assert answer.startswith(
        "2026-05 리바로가 속한 전략 시장의 시장규모는 2,139.25억원입니다."
    )
    assert "채널별 매출" not in answer
    assert "ml_006" not in answer


def test_file_sql_only_answer_is_outside_market_contract() -> None:
    original = "업로드 파일 집계 결과는 690건, 2,679,529입니다."

    answer = enforce_market_answer_contract(
        question="이 파일의 BPI를 집계해줘",
        answer=original,
        tool_calls=[
            {
                "tool": "query_uploaded_sql",
                "source": "uploaded_file_sql",
                "render_data": {"status": "ok", "rows": [{"count": 690, "sum": 2_679_529}]},
            }
        ],
    )

    assert answer == original
