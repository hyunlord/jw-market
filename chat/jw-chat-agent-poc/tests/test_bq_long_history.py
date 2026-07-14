from __future__ import annotations

from jw_chat_agent_poc.agent_loop.bq_planner import plan_bq_question
from jw_chat_agent_poc.agent_loop.periods import build_period_grounding
from jw_chat_agent_poc.agent_loop.schemas import tool_schemas
from jw_chat_agent_poc.resolver import BrandResolver
from jw_chat_agent_poc.tools.query_layer import MartRecord, StaticStrategicMartReader, StrategicQueryLayer
from jw_chat_agent_poc.tools.query_layer.catalog import default_catalog


def test_query_layer_preserves_default_window_but_allows_bq_long_history() -> None:
    layer = StrategicQueryLayer(reader=StaticStrategicMartReader((_record(),)))

    default_call = layer.brand_metric("리바로", "series", "latest", source="ubist")
    bq_call = layer.brand_metric("리바로", "series", "latest", source="ubist", history_points=60)

    assert len(default_call["render_data"]["brand_value_series_10pt"]) == 10
    assert len(bq_call["render_data"]["brand_value_series_10pt"]) == 60
    assert bq_call["render_data"]["history_points"] == 60


def test_bq_series_plan_requests_five_year_window() -> None:
    resolver = BrandResolver(mode="fixture")
    grounding = build_period_grounding("리바로 시장 규모가 지금 얼마고 어떻게 변해왔어?")
    schemas = tool_schemas(("리바로",), grounding.schema_periods, default_catalog())

    plan = plan_bq_question("리바로 시장 규모가 지금 얼마고 어떻게 변해왔어?", resolver, grounding, schemas)

    assert plan is not None
    series_calls = [call for call in plan.decision.tool_calls if call.name == "get_brand_series"]
    assert series_calls
    assert {call.arguments["history_points"] for call in series_calls} == {"60"}


def _record() -> MartRecord:
    history = {
        f"{2021 + index // 12:04d}-{index % 12 + 1:02d}": {
            "raw_value": float((index + 1) * 100_000_000),
            "ms": 1.0,
            "source_status": "OK",
        }
        for index in range(60)
    }
    return MartRecord(
        ml_id="ml_006",
        brand_name="리바로",
        source="ubist",
        measure="sales",
        metric_history=history,
        channel_data={},
        specialty_data={},
        dimension_data={},
        by_dimension={"company": "JW중외제약", "molecule": "pitavastatin"},
    )
