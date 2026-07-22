from __future__ import annotations

import pytest

from jw_chat_agent_poc.agent_loop.bq_planner import plan_bq_question
from jw_chat_agent_poc.agent_loop.bq_slots import prescription_metric_requires_typed_stop
from jw_chat_agent_poc.agent_loop.periods import build_period_grounding
from jw_chat_agent_poc.agent_loop.schemas import tool_schemas
from jw_chat_agent_poc.agent_loop.tool_helpers import metric_measure
from jw_chat_agent_poc.orchestrator.answer_contract import enforce_answer_contract
from jw_chat_agent_poc.orchestrator.answer_facts import answer_fact_markdown
from jw_chat_agent_poc.orchestrator.agent import ChatAgent
from jw_chat_agent_poc.orchestrator.markdown_response import MarkdownResponseBuilder
from jw_chat_agent_poc.resolver import BrandResolver
from jw_chat_agent_poc.tools.query_layer import (
    MartRecord,
    StaticStrategicMartReader,
    StrategicQueryLayer,
)
from jw_chat_agent_poc.tools.query_layer.catalog import default_catalog, metric_definition


def test_prescription_volume_is_an_explicit_ubist_rx_metric() -> None:
    definition = metric_definition("prescription_volume")

    assert "prescription_volume" in default_catalog().metrics
    assert definition.measure == "volume"
    assert definition.display_name == "처방량"
    assert definition.unit_label == "Rx"
    assert definition.sources == ("ubist",)


def test_prescription_volume_reads_volume_rows_without_sales_fields() -> None:
    call = _layer().brand_metric(
        "리바로젯",
        "prescription_volume",
        "latest",
        source="ubist",
    )
    data = call["render_data"]

    assert call["source"] == "UBIST"
    assert data["metric"] == "prescription_volume"
    assert data["measure"] == "volume"
    assert data["value"] == 1_500.0
    assert data["prescription_volume"] == 1_500.0
    assert data["unit_label"] == "Rx"
    assert data["value_label"] == "처방량"
    assert "sales_krw" not in data
    assert "sales_억원" not in data
    assert "매출" not in call["summary_text"]
    assert "series_insight" not in data
    assert "market_structure" not in data
    assert "hhi_recent" not in data or data["hhi_recent"] is None


def test_prescription_volume_fact_uses_volume_share_label() -> None:
    call = _layer().brand_metric(
        "리바로젯",
        "prescription_volume",
        "latest",
        source="ubist",
    )

    fact_md = answer_fact_markdown([call], ["UBIST"])

    assert "처방량 점유율" in fact_md
    assert "| 시장점유율 |" not in fact_md
    assert "| HHI |" not in fact_md


@pytest.mark.parametrize("dimension", ("channel", "specialty"))
def test_prescription_volume_breakdown_uses_volume_grain_and_rx_label(
    dimension: str,
) -> None:
    call = _layer().dimension_breakdown(
        "리바로젯",
        dimension,
        metric="prescription_volume",
        source="ubist",
    )
    data = call["render_data"]

    assert data["measure"] == "volume"
    assert data["value_label"] == "처방량"
    assert data["unit_label"] == "Rx"
    assert data["query_spec"]["metrics"] == ["prescription_volume"]
    assert data["level_segments"]
    assert all(segment["unit_label"] == "Rx" for segment in data["level_segments"])
    assert all("value_억원" not in segment for segment in data["level_segments"])


def test_iqvia_cannot_serve_ubist_prescription_volume() -> None:
    with pytest.raises(LookupError, match="prescription_volume.*iqvia_nsa|iqvia_nsa.*prescription_volume"):
        _layer().brand_metric(
            "리바로젯",
            "prescription_volume",
            "latest",
            source="iqvia_nsa",
        )


@pytest.mark.parametrize("measure", ("", "prescription_count", "rx_cnt"))
def test_blank_or_unsupported_measure_never_defaults_to_sales(measure: str) -> None:
    with pytest.raises(ValueError, match="unsupported|blank|required"):
        metric_measure(measure)


def test_sales_and_prescription_volume_cannot_be_aggregated_in_one_query() -> None:
    with pytest.raises(ValueError, match="single measure|cannot mix"):
        _layer().query(
            {
                "source": "ubist",
                "market": "ml_006",
                "metrics": ["sales", "prescription_volume"],
                "group_by": ["product"],
            },
            fallback_brand="리바로젯",
        )


def test_mixed_prescription_volume_and_sales_are_queried_separately() -> None:
    result = ChatAgent(external_mode="fixture", query_layer=_layer()).answer(
        "리바로젯 처방량과 매출을 함께 알려줘"
    )
    rendered = [
        call.get("render_data", {})
        for call in result["tool_calls"]
        if isinstance(call.get("render_data"), dict)
    ]

    assert {data.get("measure") for data in rendered} >= {"sales", "volume"}
    assert any(data.get("prescription_volume") == 1_500.0 for data in rendered)
    assert any(data.get("sales_krw") == 3_000_000_000.0 for data in rendered)
    assert not any(
        data.get("query_spec", {}).get("metrics") == ["sales", "prescription_volume"]
        for data in rendered
    )


def test_mixed_trend_keeps_iqvia_on_sales_and_volume_on_ubist() -> None:
    result = ChatAgent(external_mode="fixture", query_layer=_layer()).answer(
        "리바로젯 처방량과 매출 추이 비교"
    )
    calls = [
        call
        for call in result["tool_calls"]
        if isinstance(call.get("render_data"), dict)
    ]

    assert not any(call.get("tool") == "query_failed" for call in calls)
    assert any(call["render_data"].get("measure") == "volume" for call in calls)
    assert any(call["render_data"].get("measure") == "sales" for call in calls)
    assert not any(
        call.get("source") == "IQVIA NSA"
        and call["render_data"].get("measure") == "volume"
        for call in calls
    )


def test_c2_prescription_wording_routes_all_three_calls_to_volume() -> None:
    question = "리바로젯 진료과별 처방 추이"
    resolver = BrandResolver(mode="fixture")
    grounding = build_period_grounding(question, current_month=lambda: "2026-06")
    schemas = tool_schemas(("리바로젯",), grounding.schema_periods, default_catalog())

    plan = plan_bq_question(question, resolver, grounding, schemas)

    assert plan is not None
    assert plan.contract.contract_id == "C2"
    assert [call.name for call in plan.decision.tool_calls] == [
        "get_brand_channel_breakdown",
        "get_brand_specialty_breakdown",
        "get_brand_series",
    ]
    assert all(
        call.arguments.get("measure") == "prescription_volume"
        for call in plan.decision.tool_calls
    )


def test_s1_guard_opens_only_the_exposed_volume_metric() -> None:
    exposed = ("prescription_volume",)

    assert not prescription_metric_requires_typed_stop(
        "리바로젯 진료과별 처방 추이",
        exposed_metrics=exposed,
    )
    assert prescription_metric_requires_typed_stop(
        "리바로 처방건수 추이",
        exposed_metrics=exposed,
    )
    assert prescription_metric_requires_typed_stop(
        "리바로 처방조제액 추이",
        exposed_metrics=exposed,
    )


def test_sales_metric_behavior_stays_krw_based() -> None:
    call = _layer().brand_metric("리바로젯", "sales", "latest", source="ubist")
    data = call["render_data"]

    assert data["sales_krw"] == 3_000_000_000.0
    assert data["sales_억원"] == 30.0
    assert data["source_label"] == "UBIST"


def test_v0_specialty_output_uses_prescription_label_and_never_sales() -> None:
    call = _layer().dimension_breakdown(
        "리바로젯",
        "specialty",
        metric="prescription_volume",
        source="ubist",
    )
    response = MarkdownResponseBuilder().build(
        brand="리바로젯",
        calls=[call],
        sources=["UBIST"],
    )
    answer = enforce_answer_contract(
        "리바로젯 진료과별 처방 추이",
        response.markdown,
        {"fact_md": response.fact_md},
    )

    assert "## 진료과별 처방량 구성" in answer
    assert "처방량(Rx)" in answer
    assert "## 진료과별 매출 구성" not in answer
    assert "| 순위 | 구분 | MS | 매출 |" not in answer


def test_runtime_capability_opens_volume_but_not_unprojected_count() -> None:
    layer = _layer()

    assert layer.supports_metric("prescription_volume") is True
    assert layer.supports_metric("prescription_count") is False


def test_v0_chat_agent_preserves_prescription_volume_through_strict_and_contract_plans() -> None:
    result = ChatAgent(external_mode="fixture", query_layer=_layer()).answer(
        "리바로젯 진료과별 처방 추이"
    )

    rendered = [
        call.get("render_data", {})
        for call in result["tool_calls"]
        if isinstance(call.get("render_data"), dict)
    ]
    metric_data = [data for data in rendered if data.get("metric") == "query_spec"]

    assert metric_data
    assert all(data.get("measure") == "volume" for data in metric_data)
    assert all(data.get("query_spec", {}).get("metrics") == ["prescription_volume"] for data in metric_data)
    assert not any(data.get("measure") == "sales" for data in rendered)
    assert "처방량" in result["answer"]
    assert "Rx" in result["answer"]
    assert "진료과별 매출" not in result["answer"]
    assert "매출" not in result["answer"]


@pytest.mark.parametrize(
    "question",
    (
        "리바로젯 처방량",
        "리바로젯 처방량 추이",
        "리바로젯 유통채널별 처방량",
    ),
)
def test_direct_and_channel_prescription_volume_questions_never_fall_back_to_sales(
    question: str,
) -> None:
    result = ChatAgent(external_mode="fixture", query_layer=_layer()).answer(question)
    rendered = [
        call.get("render_data", {})
        for call in result["tool_calls"]
        if isinstance(call.get("render_data"), dict)
    ]

    assert rendered
    assert any(data.get("measure") == "volume" for data in rendered)
    assert not any(data.get("measure") == "sales" for data in rendered)
    assert "처방량" in result["answer"]


def _layer() -> StrategicQueryLayer:
    return StrategicQueryLayer(reader=StaticStrategicMartReader(_records()))


def _records() -> tuple[MartRecord, ...]:
    return (
        _record("리바로젯", "ubist", "sales", 3_000_000_000.0),
        _record("대조브랜드", "ubist", "sales", 7_000_000_000.0),
        _record("리바로젯", "iqvia_nsa", "sales", 4_000_000_000.0),
        _record("대조브랜드", "iqvia_nsa", "sales", 6_000_000_000.0),
        _record("리바로젯", "ubist", "volume", 1_500.0),
        _record("대조브랜드", "ubist", "volume", 3_500.0),
    )


def _record(brand: str, source: str, measure: str, value: float) -> MartRecord:
    history = {
        "2026-04": {"raw_value": value * 0.8, "source_status": "OK"},
        "2026-05": {"raw_value": value, "source_status": "OK"},
    }
    ratio = value / (1_500.0 if brand == "리바로젯" else 3_500.0)
    channel_data = (
        {
            "의원": {"2026-05": {"raw_value": 900.0 * ratio}},
            "종합병원": {"2026-05": {"raw_value": 600.0 * ratio}},
        }
        if measure == "volume"
        else {
            "의원": {"2026-05": {"raw_value": value * 0.6}},
            "종합병원": {"2026-05": {"raw_value": value * 0.4}},
        }
    )
    specialty_data = (
        {
            "순환기내과": {"2026-05": {"raw_value": 1_000.0 * ratio}},
            "내분비내과": {"2026-05": {"raw_value": 500.0 * ratio}},
        }
        if measure == "volume"
        else {
            "순환기내과": {"2026-05": {"raw_value": value * 0.7}},
            "내분비내과": {"2026-05": {"raw_value": value * 0.3}},
        }
    )
    return MartRecord(
        ml_id="ml_006",
        brand_name=brand,
        source=source,
        measure=measure,
        metric_history=history,
        channel_data=channel_data,
        specialty_data=specialty_data,
        dimension_data={},
        by_dimension={"company": "테스트제약", "molecule": "테스트성분"},
    )
