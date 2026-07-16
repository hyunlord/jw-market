from __future__ import annotations

import pytest

from jw_chat_agent_poc.orchestrator.market_insights import (
    forbidden_claims,
    render_market_insights,
    render_market_narrative,
)
from jw_chat_agent_poc.orchestrator.markdown_renderers import series_md
from jw_chat_agent_poc.orchestrator.markdown_response import MarkdownResponseBuilder
from jw_chat_agent_poc.orchestrator.provenance import (
    EvidenceFact,
    evidence_from_calls,
    verify_markdown_numbers,
)
from jw_chat_agent_poc.service.genos_client import GenosClient
from jw_chat_agent_poc.tools.query_layer import MartRecord, StaticStrategicMartReader, StrategicQueryLayer


def test_series_answer_combines_context_columns_and_mechanical_insights() -> None:
    call = _layer().brand_metric("리바로", "series", "latest")

    responses = tuple(
        MarkdownResponseBuilder().build(brand="리바로", calls=[call], sources=["UBIST"])
        for _ in range(5)
    )

    markdown = responses[0].markdown
    assert all(response.markdown == markdown for response in responses)
    assert "| 기간 | 시장점유율(%) | 처방조제액(억원) | 시장규모(억원) |" in markdown
    assert "점유율은 20.00%에서 19.35%로 0.65%p 감소" in markdown
    assert "처방조제액은 0.80억원에서 0.84억원으로 0.04억원 증가" in markdown
    assert "브랜드 성장률 5.00%" in markdown
    assert "시장 성장률 8.50%" in markdown
    assert "초과성장 -3.50%p" in markdown
    assert responses[0].verification["status"] == "pass"
    assert forbidden_claims(markdown) == ()


def test_market_narrative_explains_verified_growth_gap_without_inventing_a_cause() -> None:
    call = _layer().brand_metric("리바로", "series", "latest")
    facts = evidence_from_calls([call], "")

    narrative = render_market_narrative([call])

    assert narrative.startswith(
        "리바로는 매출이 늘었지만 점유율은 낮아져, 외형 성장과 시장 내 상대적 위치가 엇갈렸습니다."
    )
    assert "시장 성장 속도에는 못 미쳤습니다" in narrative
    assert "점유율은 20.00%에서 19.35%로 0.65%p 감소" in narrative
    assert "처방조제액은 0.80억원에서 0.84억원으로 0.04억원 증가" in narrative
    assert "브랜드 성장률 5.00% · 시장 성장률 8.50% · 초과성장 -3.50%p" in narrative
    assert verify_markdown_numbers(narrative, facts).status == "pass"
    assert forbidden_claims(narrative) == ()


def test_market_narrative_returns_empty_when_no_interpretable_evidence_exists() -> None:
    call = {"render_data": {"brand": "리바로", "series_insight": {}}}

    assert render_market_narrative([call]) == ""


def test_competitor_delta_evidence_requires_both_source_operands() -> None:
    complete = {
        "tool": "get_brand_metric",
        "render_data": {
            "series_insight": {
                "competitors": [
                    {
                        "brand": "로수젯",
                        "share_start_pct": 50.0,
                        "share_end_pct": 50.69124423963134,
                        "sales_start_krw": 200_000_000.0,
                        "sales_end_krw": 220_000_000.0,
                    }
                ]
            }
        },
    }
    missing_start = {
        "tool": "get_brand_metric",
        "render_data": {
            "series_insight": {
                "competitors": [
                    {
                        "brand": "로수젯",
                        "share_end_pct": 50.69124423963134,
                        "sales_end_krw": 220_000_000.0,
                    }
                ]
            }
        },
    }

    complete_values = {fact.value for fact in evidence_from_calls([complete], "")}
    missing_values = {fact.value for fact in evidence_from_calls([missing_start], "")}

    assert "0.69%p" in complete_values
    assert "0.20억원" in complete_values
    assert "0.69%p" not in missing_values
    assert "0.20억원" not in missing_values


def test_genos_final_answer_places_rich_verified_narrative_before_existing_table(monkeypatch) -> None:
    call = _layer().brand_metric("리바로", "series", "latest")
    response = MarkdownResponseBuilder().build(brand="리바로", calls=[call], sources=["UBIST"])
    monkeypatch.setattr(GenosClient, "_chat_text", lambda *_args: response.data_md)

    answer = GenosClient(token="dummy-token")._markdown_answer(
        "리바로 요즘 상황",
        response.to_dict(),
        tool_calls=[call],
    )
    facts = evidence_from_calls([call], response.data_md)

    narrative = "리바로는 매출이 늘었지만 점유율은 낮아져, 외형 성장과 시장 내 상대적 위치가 엇갈렸습니다."
    assert answer.startswith(narrative)
    assert narrative in answer
    assert "시장 성장 속도에는 못 미쳤습니다" in answer
    assert "수치로 보면" in answer
    first_table = next(line for line in answer.splitlines() if line.startswith("|"))
    assert answer.index(narrative) < answer.index(first_table)
    assert verify_markdown_numbers(answer, facts).status == "pass"
    assert forbidden_claims(answer) == ()


def test_exact_single_period_question_does_not_receive_trend_narrative(monkeypatch) -> None:
    call = _layer().brand_metric("리바로", "series", "latest")
    response = MarkdownResponseBuilder().build(brand="리바로", calls=[call], sources=["UBIST"])
    monkeypatch.setattr(GenosClient, "_chat_text", lambda *_args: response.data_md)

    answer = GenosClient(token="dummy-token")._markdown_answer(
        "리바로 2025-Q2 매출 얼마?",
        response.to_dict(),
        tool_calls=[call],
    )

    assert "외형 성장과 시장 내 상대적 위치" not in answer


def test_missing_market_period_never_renders_zero_or_negative_hundred_growth() -> None:
    call = _layer(missing_market_period=True).brand_metric("리바로", "series", "latest")

    response = MarkdownResponseBuilder().build(brand="리바로", calls=[call], sources=["UBIST"])

    assert "0.00억원" not in response.markdown
    assert "-100%" not in response.markdown
    assert "2026-02은 데이터 미보유" in response.markdown


def test_numeric_and_claim_injection_fail_closed_but_normal_answer_passes() -> None:
    call = _layer().brand_metric("리바로", "series", "latest")
    response = MarkdownResponseBuilder().build(brand="리바로", calls=[call], sources=["UBIST"])
    facts = evidence_from_calls([call], response.data_md)

    assert verify_markdown_numbers(response.markdown, facts).status == "pass"
    assert verify_markdown_numbers(response.markdown + "\n- 근거 없는 값 29.53%", facts).status == "fail"
    assert forbidden_claims(response.markdown + "\n- 경쟁 심화로 하락했습니다.") == ("경쟁 심화",)


def test_missing_value_and_rounding_boundary_injections_fail_closed() -> None:
    missing_call = _layer(missing_market_period=True).brand_metric("리바로", "series", "latest")
    missing_response = MarkdownResponseBuilder().build(
        brand="리바로",
        calls=[missing_call],
        sources=["UBIST"],
    )
    missing_facts = evidence_from_calls([missing_call], missing_response.data_md)
    boundary_fact = EvidenceFact(
        fact_id="cr5",
        label="CR5",
        value="29.52%",
        source="UBIST",
        tool="get_top_brands",
        path="render_data.series_insight.cr5_end_pct",
        period="2026-05",
        allowed_numbers=("CR5", "29.52%"),
    )

    assert verify_markdown_numbers(missing_response.markdown + "\n- 시장 성장률 -100%", missing_facts).status == "fail"
    assert verify_markdown_numbers("CR5 29.52%", (boundary_fact,)).status == "pass"
    assert verify_markdown_numbers("CR5 29.53%", (boundary_fact,)).status == "fail"


def test_combined_series_table_tolerates_explicitly_missing_market_series() -> None:
    markdown = series_md(
        {
            "brand_value_series_10pt": [
                {"period": "2026-05", "value_krw": 8_000_000_000, "ms_pct": 3.76},
            ],
            "market_size_series": None,
        }
    )

    assert "| 2026-05 | 3.76% | 80.00억원 | — |" in markdown


def test_renderer_does_not_emit_non_finite_insight_numbers() -> None:
    insight = {
        "share_start_pct": float("nan"),
        "share_end_pct": 3.76,
        "share_delta_pctp": float("inf"),
        "sales_start_krw": 8_000_000_000.0,
        "sales_end_krw": 8_400_000_000.0,
        "sales_delta_krw": 400_000_000.0,
    }

    markdown = "\n".join(
        render_market_insights([{"render_data": {"brand": "리바로", "series_insight": insight}}])
    )

    assert "nan" not in markdown.lower()
    assert "inf" not in markdown.lower()


@pytest.mark.parametrize(
    ("insight", "expected"),
    (
        (
            {
                "share_start_pct": 4.0,
                "share_end_pct": 3.8,
                "share_delta_pctp": -0.2,
                "sales_start_krw": 8_000_000_000.0,
                "sales_end_krw": 8_400_000_000.0,
                "sales_delta_krw": 400_000_000.0,
            },
            "점유율은 4.00%에서 3.80%로 0.20%p 감소했으나",
        ),
        (
            {
                "share_start_pct": 3.8,
                "share_end_pct": 4.0,
                "share_delta_pctp": 0.2,
                "sales_start_krw": 8_400_000_000.0,
                "sales_end_krw": 8_000_000_000.0,
                "sales_delta_krw": -400_000_000.0,
            },
            "점유율은 3.80%에서 4.00%로 0.20%p 증가했으나",
        ),
        (
            {"brand_growth_pct": 6.6, "market_growth_pct": 12.4, "excess_growth_pctp": -5.8},
            "초과성장 -5.80%p",
        ),
        ({"rank_start": 8, "rank_end": 9}, "순위는 8위에서 9위로 변했습니다"),
        (
            {
                "share_max_pct": 3.93,
                "share_max_period": "2025-08",
                "share_min_pct": 3.75,
                "share_min_period": "2026-04",
            },
            "최고 3.93%(2025-08) · 최저 3.75%(2026-04)",
        ),
        ({"turning_point": "2025-10", "turning_kind": "low"}, "2025-10 저점 후 반등"),
        ({"trend_direction": "down", "trend_months": 3}, "최근 3개월 연속 하락"),
        (
            {
                "competitors": [
                    {
                        "brand": "로수젯",
                        "share_start_pct": 9.13,
                        "share_end_pct": 9.42,
                        "sales_end_krw": 19_523_856_200.0,
                        "rank_end": 1,
                    }
                ]
            },
            "같은 기간 로수젯의 점유율은 9.13%에서 9.42%",
        ),
        (
            {"hhi_end": 253.62, "cr5_end_pct": 29.515799, "denominator_end": 555},
            "상위 5개 합계 29.52% · HHI 253.62",
        ),
        ({"missing_periods": ["2025-09"]}, "2025-09은 데이터 미보유"),
    ),
)
def test_each_mechanical_insight_rule_only_uses_supplied_evidence(
    insight: dict[str, object],
    expected: str,
) -> None:
    markdown = "\n".join(
        render_market_insights([{"render_data": {"brand": "리바로", "series_insight": insight}}])
    )

    assert expected in markdown
    assert forbidden_claims(markdown) == ()


def test_empty_insight_does_not_fill_the_answer_with_invented_claims() -> None:
    lines = render_market_insights([{"render_data": {"brand": "리바로", "series_insight": {}}}])

    assert lines == ()


def _layer(*, missing_market_period: bool = False) -> StrategicQueryLayer:
    return StrategicQueryLayer(reader=StaticStrategicMartReader(_records(missing_market_period)))


def _records(missing_market_period: bool) -> tuple[MartRecord, ...]:
    periods = ("2026-01", "2026-02", "2026-03")
    values = {
        "로수젯": (2.00, 2.10, 2.20),
        "리피토": (1.20, None if missing_market_period else 1.25, 1.30),
        "리바로": (0.80, 0.82, 0.84),
    }
    totals = {
        period: None
        if any(values[brand][index] is None for brand in values)
        else sum(float(values[brand][index]) for brand in values)
        for index, period in enumerate(periods)
    }
    return tuple(_record(brand, series, periods, totals) for brand, series in values.items())


def _record(
    brand: str,
    values: tuple[float | None, ...],
    periods: tuple[str, ...],
    totals: dict[str, float | None],
) -> MartRecord:
    history = {}
    for index, period in enumerate(periods):
        value = values[index]
        total = totals[period]
        history[period] = {
            "raw_value": value * 100_000_000 if value is not None else None,
            "ms": value / total * 100 if value is not None and total else None,
            "source_status": "OK" if value is not None else "missing",
        }
    return MartRecord(
        ml_id="ml_006",
        brand_name=brand,
        source="ubist",
        measure="sales",
        metric_history=history,
        channel_data={},
        specialty_data={},
        dimension_data={},
        by_dimension={"company": "테스트제약", "molecule": f"{brand}성분"},
    )
