from __future__ import annotations

from dataclasses import replace

from jw_chat_agent_poc.agent_loop.tools import AgentToolFacade
from jw_chat_agent_poc.resolver import BrandResolver
from jw_chat_agent_poc.tools.metrics import MetricsTool
from jw_chat_agent_poc.tools.query_layer import (
    MartRecord,
    StaticStrategicMartReader,
    StrategicQueryLayer,
)


def _record(source: str, value: float) -> MartRecord:
    return MartRecord(
        ml_id="ml_006",
        brand_name="리바로",
        source=source,
        measure="sales",
        metric_history={
            "2026-05" if source == "ubist" else "2026-Q2": {
                "raw_value": value,
                "ms": 10.0,
                "source_status": "OK",
            }
        },
        channel_data={},
        specialty_data={},
        dimension_data={},
        by_dimension={"company": "JW중외제약", "molecule": "피타바스타틴"},
    )


def _layer() -> StrategicQueryLayer:
    return StrategicQueryLayer(
        reader=StaticStrategicMartReader(
            (
                _record("ubist", 8_000_000_000.0),
                replace(_record("iqvia_nsa", 9_000_000_000.0), source="iqvia_nsa"),
            )
        )
    )


def test_query_layer_can_select_each_source_without_aggregation() -> None:
    layer = _layer()

    ubist = layer.brand_metric("리바로", "series", "latest", source="ubist")
    iqvia = layer.brand_metric("리바로", "series", "latest", source="iqvia_nsa")

    assert ubist["source"] == "UBIST"
    assert ubist["render_data"]["sales_krw"] == 8_000_000_000.0
    assert iqvia["source"] == "IQVIA"
    assert iqvia["render_data"]["sales_krw"] == 9_000_000_000.0


def test_agent_facade_forwards_explicit_source_to_query_layer() -> None:
    facade = AgentToolFacade(
        metrics=MetricsTool(mode="fixture"),
        resolver=BrandResolver(mode="fixture"),
        allowed_brands=("리바로",),
        query_layer=_layer(),
    )

    result = facade.execute(
        "get_brand_series",
        {"brand": "리바로", "period": "latest", "source": "iqvia_nsa"},
    )

    assert result.status == "ok"
    assert result.call["source"] == "IQVIA"
    assert result.call["render_data"]["query_spec"]["source"] == "iqvia_nsa"


def test_top_brands_forwards_explicit_source_without_substitution() -> None:
    layer = _layer()
    facade = AgentToolFacade(
        metrics=MetricsTool(mode="fixture"),
        resolver=BrandResolver(mode="fixture"),
        allowed_brands=("리바로",),
        query_layer=layer,
    )

    result = facade.execute(
        "get_top_brands",
        {"brand": "리바로", "limit": "5", "source": "iqvia_nsa"},
    )

    assert result.status == "ok"
    assert result.call["source"] == "IQVIA"
    assert result.call["render_data"]["query_spec"]["source"] == "iqvia_nsa"
