from __future__ import annotations

from jw_chat_agent_poc.tool_use.v3_execution_contracts import (
    MarketMetricFact,
    ToolFailureRecord,
    V3EvidenceBundle,
)
from jw_chat_agent_poc.tool_use.v3_selection import MultiToolChoice
from jw_chat_agent_poc.tool_use.market_scope_projection import rounded_hhi


def _fact(
    evidence_id: str,
    tool_name: str,
    render_data: dict[str, object],
) -> MarketMetricFact:
    return MarketMetricFact(
        evidence_id=evidence_id,
        tool_name=tool_name,
        arguments={"brand": "리바로"},
        raw_result={"render_data": render_data},
        missing_required_fields=(),
        entity="리바로",
        metric="sales",
        period="2026-Q1",
        unit="억원",
        view="general",
        market="C10A1",
    )


def _bundle(
    *facts: MarketMetricFact,
    failures: tuple[ToolFailureRecord, ...] = (),
) -> V3EvidenceBundle:
    return V3EvidenceBundle(
        status="partial" if facts and failures else "complete" if facts else "failed",
        facts=facts,
        failures=failures,
        deferred=(),
        executions=(),
        original_call_count=len(facts) + len(failures),
        executed_call_count=len(facts) + len(failures),
        deduplicated_call_count=0,
    )


def _view_facts() -> tuple[MarketMetricFact, ...]:
    shared = {
        "brand": "리바로",
        "period": "2026-Q1",
        "market_size_period": "2026-Q1",
        "hhi_period": "2026-Q1",
        "market_size_series": (
            {"period": "2025-Q1", "value": 100.0},
            {"period": "2026-Q1", "value": 104.85996797321597},
        ),
        "market_growth_series": (
            {"period": "2025-Q1", "yoy_growth_pct": 3.0},
            {"period": "2026-Q1", "yoy_growth_pct": 4.859967973215973},
        ),
        "hhi_series_5y": (
            {"period": "2025-Q1", "hhi": 3015.4124533412323},
            {"period": "2026-Q1", "hhi": 3188.040362260885},
        ),
        "brand_ranking_stacked": (
            {"period": "2026-Q1", "brand": "리바로", "ms": 3.7644, "rank": 6},
            {"period": "2026-Q1", "brand": "리피토", "ms": 8.1, "rank": 1},
        ),
        "brand_sales_krw": 8_039_000_000,
        "market_share": 3.7644,
        "rank": 6,
    }
    growth = {
        "brand": "리바로",
        "period": "2026-Q1",
        "value": {
            "market_growth_pct": 4.859967973215973,
            "growth_contribution_pct": -0.5518,
        },
    }
    channel = {
        "brand": "리바로",
        "period": "2026-Q1",
        "target_customer_competition_by_channel": {
            "views": [
                {
                    "target_name": "종합병원",
                    "periods": ["2026-Q1"],
                    "trend_brands": [
                        {
                            "brand": "리바로",
                            "value_series": [30.0],
                            "ms_series": [12.3],
                            "rank_series": [2],
                        }
                    ],
                }
            ]
        },
    }
    return (
        _fact("v3-shadow:market.get_brand_metric:1111", "market.get_brand_metric", shared),
        _fact(
            "v3-shadow:market.get_growth_contribution:2222",
            "market.get_growth_contribution",
            growth,
        ),
        _fact(
            "v3-shadow:market.get_channel_breakdown:3333",
            "market.get_channel_breakdown",
            channel,
        ),
    )


def test_scope_view_choices_reuse_selector_scope_without_question_mapping() -> None:
    from jw_chat_agent_poc.tool_use.v3_scope_view_set import scope_view_choices

    selected = (
        MultiToolChoice(
            "market.get_definition",
            {
                "brand": "리바로",
                "market_id": "ml_006",
                "view": "market_landscape",
            },
        ),
    )

    choices = scope_view_choices(selected, scope_confirmed=True)

    assert [choice.name for choice in choices] == [
        "market.get_brand_metric",
        "market.get_hhi",
        "market.get_growth_contribution",
        "market.get_channel_breakdown",
    ]
    assert all(choice.arguments["brand"] == "리바로" for choice in choices)
    assert all(choice.arguments["market"] == "ml_006" for choice in choices)
    assert all(choice.arguments["view"] == "strategic" for choice in choices)


def test_scope_view_choices_require_a_brand_anchor() -> None:
    from jw_chat_agent_poc.tool_use.v3_scope_view_set import scope_view_choices

    selected = (MultiToolChoice("market.get_definition", {"market_id": "ml_006"}),)

    assert scope_view_choices(selected, scope_confirmed=True) == ()


def test_scope_view_choices_reject_selector_invented_anchor() -> None:
    from jw_chat_agent_poc.tool_use.v3_scope_view_set import scope_view_choices

    selected = (
        MultiToolChoice("market.get_hhi", {"brand": "리바로"}),
    )

    assert scope_view_choices(selected, scope_confirmed=False) == ()


def test_complete_grounded_bundle_renders_six_views_and_supported_charts() -> None:
    from jw_chat_agent_poc.tool_use.v3_scope_view_set import build_scope_view_set

    result = build_scope_view_set(_bundle(*_view_facts()), scope_confirmed=True)

    assert result.attached is True
    assert result.view_names == (
        "시장 규모 및 성장률 추이",
        "HHI 추이",
        "브랜드 순위",
        "리바로 매출·점유율·순위",
        "시장 성장 기여도",
        "채널별 구성",
    )
    assert result.markdown.startswith("---\n\n## 시장 기본 뷰")
    assert "| 기간 | 시장 규모 | 성장률(%) |" not in result.markdown
    assert "| 기간 | HHI |" not in result.markdown
    assert "시계열은 차트로 표시했습니다." in result.markdown
    assert "4.9" in result.markdown
    assert "3.76" in result.markdown
    assert "80.39억원" in result.markdown
    assert "3015.4124533412323" not in result.markdown
    assert {chart["type"] for chart in result.charts} <= {"line", "bar", "doughnut"}
    assert len(result.charts) >= 3


def test_partial_failure_preserves_successful_views_and_adds_limitation() -> None:
    from jw_chat_agent_poc.tool_use.v3_scope_view_set import build_scope_view_set

    facts = _view_facts()[:1]
    failure = ToolFailureRecord(
        "market.get_channel_breakdown",
        {"brand": "리바로"},
        "execute",
        "NO_DATA",
        "channel data unavailable",
    )

    result = build_scope_view_set(
        _bundle(*facts, failures=(failure,)),
        scope_confirmed=True,
    )

    assert result.attached is True
    assert "시장 규모 및 성장률 추이" in result.view_names
    assert "채널별 구성" not in result.view_names
    assert any("채널별 구성" in item for item in result.limitations)


def test_dashboard_table_series_shape_renders_without_top_level_market_size() -> None:
    from jw_chat_agent_poc.tool_use.v3_scope_view_set import build_scope_view_set

    fact = _fact(
        "v3-shadow:market.get_brand_metric:dashboard",
        "market.get_brand_metric",
        {
            "brand": "리바로",
            "period": "2026-Q1",
            "dashboard_tables": (
                {
                    "name": "시장 규모 및 성장 추이",
                    "columns": ("기간", "시장 규모", "성장률(%)", "단위"),
                    "rows": (
                        ("2025-Q1", 100.0, 3.0, "억원"),
                        ("2026-Q1", 104.85996797321597, 4.859967973215973, "억원"),
                    ),
                },
            ),
        },
    )

    result = build_scope_view_set(_bundle(fact), scope_confirmed=True)

    assert result.attached is True
    assert result.view_names == ("시장 규모 및 성장률 추이",)
    assert "| 기간 | 시장 규모 | 성장률(%) |" not in result.markdown
    assert "104.85996797321597" not in result.markdown
    assert result.charts[0]["labels"] == ["2025-Q1", "2026-Q1"]


def test_ungrounded_series_chart_falls_back_to_at_most_twelve_formatted_rows() -> None:
    from jw_chat_agent_poc.tool_use.v3_scope_view_set import build_scope_view_set

    series = tuple(
        {
            "period": f"2025-{month:02d}",
            "value": 14_391_478_628.907 + month,
            "yoy_growth_pct": 4.859967973215973,
        }
        for month in range(1, 13)
    ) + tuple(
        {
            "period": f"2026-{month:02d}",
            "value": 14_469_561_923.530005 + month,
            "yoy_growth_pct": 4.859967973215973,
        }
        for month in range(1, 13)
    )
    fact = _fact(
        "v3-shadow:market.get_brand_metric:fallback",
        "market.get_brand_metric",
        {"brand": "리바로", "period": "2026-12", "market_size_series": series},
    )

    result = build_scope_view_set(
        _bundle(fact),
        scope_confirmed=True,
        chart_numeric_override=999_999_999_999.0,
    )

    table_rows = [line for line in result.markdown.splitlines() if line.startswith("| 2026-")]
    assert len(table_rows) == 12
    assert "최근 12개월" in result.markdown
    assert "억원" in result.markdown
    assert ".530005" not in result.markdown


def test_ungrounded_section_is_skipped_without_discarding_other_views() -> None:
    from jw_chat_agent_poc.tool_use.v3_scope_view_set import build_scope_view_set

    grounded = _view_facts()[0]
    malformed_growth = _fact(
        "v3-shadow:market.get_growth_contribution:bad",
        "market.get_growth_contribution",
        {
            "brand": "리바로",
            "period": "2026-Q1",
            "value": {"market_growth_pct": object()},
        },
    )

    result = build_scope_view_set(
        _bundle(grounded, malformed_growth),
        scope_confirmed=True,
    )

    assert result.attached is True
    assert "시장 규모 및 성장률 추이" in result.view_names
    assert "시장 성장 기여도" not in result.view_names
    assert any("시장 성장 기여도" in item for item in result.limitations)


def test_failed_or_ungrounded_bundle_never_attaches_a_view() -> None:
    from jw_chat_agent_poc.tool_use.v3_scope_view_set import build_scope_view_set

    failed = _bundle(
        failures=(
            ToolFailureRecord(
                "market.get_brand_metric",
                {"brand": "존재하지않는브랜드XYZ987654"},
                "execute",
                "UNKNOWN_BRAND",
                "unknown brand",
            ),
        )
    )
    ungrounded = _fact(
        "v3-shadow:market.get_brand_metric:bad",
        "market.get_brand_metric",
        {
            "brand": "리바로",
            "period": "2026-Q1",
            "market_size_series": ({"period": "2026-Q1", "value": 100.0},),
        },
    )

    failed_result = build_scope_view_set(failed, scope_confirmed=True)
    assert failed_result.attached is False
    assert failed_result.limitations == (
        "시장 규모 및 성장률 추이 데이터는 확인하지 못했습니다.",
        "브랜드 순위 데이터는 확인하지 못했습니다.",
        "대상 브랜드 매출·점유율·순위 데이터는 확인하지 못했습니다.",
    )
    grounded_table_only = build_scope_view_set(
        _bundle(ungrounded),
        scope_confirmed=True,
        chart_numeric_override=999.0,
    )
    assert grounded_table_only.attached is True
    assert grounded_table_only.charts == ()
    assert "| 2026-Q1 | 100.00억원 |" in grounded_table_only.markdown
    assert "근거와 결속되지 않은 차트는 제외했습니다." in grounded_table_only.limitations


def test_unconfirmed_scope_never_renders_selector_primary_evidence() -> None:
    from jw_chat_agent_poc.tool_use.v3_scope_view_set import build_scope_view_set

    result = build_scope_view_set(_bundle(*_view_facts()), scope_confirmed=False)

    assert result.attached is False


def test_successful_view_removes_only_its_stale_failure_limitation() -> None:
    from jw_chat_agent_poc.tool_use.v3_scope_view_set import reconcile_view_limitations

    limitations = (
        "시장 성장 기여도 데이터는 확인하지 못했습니다.",
        "채널별 구성 데이터는 확인하지 못했습니다.",
        "외부 근거의 기간이 다릅니다.",
    )

    assert reconcile_view_limitations(limitations, ("시장 성장 기여도",)) == (
        "채널별 구성 데이터는 확인하지 못했습니다.",
        "외부 근거의 기간이 다릅니다.",
    )


def test_failed_view_keeps_its_failure_limitation() -> None:
    from jw_chat_agent_poc.tool_use.v3_scope_view_set import reconcile_view_limitations

    limitation = "시장 성장 기여도 데이터는 확인하지 못했습니다."

    assert reconcile_view_limitations((limitation,), ()) == (limitation,)


def test_six_iqvia_market_hhi_display_values_use_rounding() -> None:
    observed = {
        "S01P0": (3188.040362260885, 3188.0404),
        "A02A2": (3015.4124533412323, 3015.4125),
        "A05A2": (5652.065915370253, 5652.0659),
        "D06A0": (2773.840547521344, 2773.8405),
        "N05C0": (717.6910084589456, 717.691),
        "D04A0": (1092.7497212632295, 1092.7497),
    }

    assert {market: rounded_hhi(raw) for market, (raw, _) in observed.items()} == {
        market: expected for market, (_, expected) in observed.items()
    }
