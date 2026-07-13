from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

import pytest

from jw_chat_agent_poc import ChatAgent
from jw_chat_agent_poc.agent_loop import should_use_agent_loop
from jw_chat_agent_poc.agent_loop.loop import ToolUseAgent
from jw_chat_agent_poc.agent_loop.models import AgentDecision, ToolCallPlan
from jw_chat_agent_poc.agent_loop.population_specs import strict_query_plan
from jw_chat_agent_poc.agent_loop.schemas import tool_schemas
from jw_chat_agent_poc.agent_loop.tools import AgentToolFacade
from jw_chat_agent_poc.orchestrator.answer_facts import answer_fact_markdown
from jw_chat_agent_poc.resolver import BrandResolver
from jw_chat_agent_poc.tools.external.client import ExternalCall
from jw_chat_agent_poc.tools.metrics import MetricsTool
from jw_chat_agent_poc.tools.metrics.cache_live import StaticCausePayloadReader, StaticMetricsCacheReader
from jw_chat_agent_poc.tools.query_layer import MartRecord, StaticStrategicMartReader, StrategicQueryLayer
from jw_chat_agent_poc.tools.query_layer.market_structure import market_structure

from test_metrics_cache import BRAND_CARDS, CACHE_BRANDS


@dataclass(slots=True)
class ScriptedPlanner:
    decisions: tuple[AgentDecision, ...]
    index: int = 0
    schema_history: list[tuple[dict[str, Any], ...]] | None = None

    def decide(
        self,
        _question: str,
        _observations,
        schemas: tuple[dict[str, Any], ...],
        allowed_brands: tuple[str, ...] = (),
        allowed_periods: tuple[str, ...] = (),
    ) -> AgentDecision:
        if self.schema_history is None:
            self.schema_history = []
        self.schema_history.append(schemas)
        decision = self.decisions[min(self.index, len(self.decisions) - 1)]
        self.index += 1
        return decision


class PatentEchoExternal:
    def mfds_patent(self, ingredient_en: str) -> ExternalCall:
        return ExternalCall(
            tool="mfds_patent",
            source="external_api",
            status="ok",
            summary_text=f"{ingredient_en} MFDS patent echo",
            render_data={"query": ingredient_en, "items": []},
        )

    def mfds_fda_orangebook(self, ingredient_en: str) -> ExternalCall:
        return ExternalCall(
            tool="mfds_fda_orangebook",
            source="external_api",
            status="ok",
            summary_text=f"{ingredient_en} OrangeBook echo",
            render_data={"query": ingredient_en, "items": []},
        )


def test_query_schema_injects_market_catalog_enums() -> None:
    """Given a strategic market, tool schemas expose only catalog-valid identifiers."""

    layer = _query_layer()
    catalog = layer.catalog_for_brand("리바로")

    schemas = tool_schemas(("리바로",), ("latest", "2026-04"), catalog)

    query_schema = next(schema for schema in schemas if schema["function"]["name"] == "query")
    dimension_enum = query_schema["function"]["parameters"]["properties"]["spec"]["properties"]["dimensions"]["items"]["enum"]
    assert "product" in dimension_enum
    assert "company" in dimension_enum
    assert "nhi_type" not in dimension_enum


def test_query_catalog_exposes_class2_only_for_split_market() -> None:
    """Given a dual-class market, catalog preserves split metadata while exposing Class 2 for grouping."""

    layer = StrategicQueryLayer(reader=StaticStrategicMartReader(_split_class_records()))

    catalog = layer.catalog_for_brand("악템라")

    assert "class_2" in catalog.dimensions
    assert "class_1" not in catalog.dimensions
    assert "dosage_form" not in catalog.dimensions
    assert catalog.market_structure["type"] == "class_split"
    assert catalog.market_structure["display_axis"] == "class_2"
    assert {axis["key"] for axis in catalog.market_structure["axes"]} == {"class_1", "class_2"}


def test_market_structure_falls_back_to_sibling_sources_for_split_metadata() -> None:
    """Given one source lacks class columns, market structure is still inferred from sibling source rows."""

    snapshot = StaticStrategicMartReader(
        (
            *_split_class_records(),
            *_split_market_ubist_records_without_class(),
        )
    ).load()

    structure = market_structure(snapshot, "ml_011", "ubist")

    assert structure["type"] == "class_split"
    assert structure["display_axis"] == "class_2"


def test_brand_metric_carries_split_market_structure_for_source_detail() -> None:
    """Given a split market metric, source facts still show the Class 2 operating basis."""

    layer = StrategicQueryLayer(reader=StaticStrategicMartReader(_split_class_records()))

    call = layer.brand_metric("악템라", "sales", "latest")

    data = call["render_data"]
    assert data["market_structure"]["type"] == "class_split"
    assert data["market_structure"]["display_axis"] == "class_2"
    fact_md = answer_fact_markdown([call], ["IQVIA NSA"])
    assert "Class 구분 존재" in fact_md
    assert "Class 2 기준" in fact_md


def test_brand_metric_uses_the_source_that_contains_an_iqvia_only_brand() -> None:
    """Given a mixed-source market, an IQVIA-only brand must not inherit the market UBIST default."""

    periods = ("2026-Q1",)
    layer = StrategicQueryLayer(
        reader=StaticStrategicMartReader(
            (
                replace(_record_with_market("ml_003", "UBIST브랜드", (100.0,), periods, [100.0]), source="ubist"),
                replace(_record_with_market("ml_003", "마운자로", (200.0,), periods, [200.0]), source="iqvia_nsa"),
            )
        )
    )

    call = layer.brand_metric("마운자로", "market_share", "latest")

    assert call["source"] == "IQVIA"
    assert call["render_data"]["brand"] == "마운자로"


def test_brand_series_keeps_latest_alias_until_iqvia_source_is_selected() -> None:
    """The facade must not turn ``latest`` into a monthly period before source selection."""

    periods = ("2025-Q4", "2026-Q1")
    layer = StrategicQueryLayer(
        reader=StaticStrategicMartReader(
            (
                replace(_record_with_market("ml_003", "UBIST브랜드", (100.0, 110.0), periods, [100.0, 110.0]), source="ubist"),
                replace(_record_with_market("ml_003", "마운자로", (180.0, 200.0), periods, [180.0, 200.0]), source="iqvia_nsa"),
            )
        )
    )
    facade = AgentToolFacade(
        metrics=_metrics_tool(),
        resolver=BrandResolver(),
        allowed_brands=("마운자로",),
        query_layer=layer,
    )

    execution = facade.execute("get_brand_series", {"brand": "마운자로", "period": "latest"})

    assert execution.status == "ok"
    assert execution.call["source"] == "IQVIA"
    assert execution.call["tool"] == "get_brand_metric"
    assert execution.call["render_data"]["period"] == "2026-Q1"


def test_metric_query_failure_does_not_fall_through_to_fixture_zero() -> None:
    class BrokenLayer:
        def brand_metric(self, brand: str, metric: str, period: str) -> dict[str, Any]:
            raise LookupError(f"missing {brand} {metric} {period}")

        def catalog_for_brand(self, brand: str | None):
            return None

    facade = AgentToolFacade(
        metrics=_metrics_tool(),
        resolver=BrandResolver(),
        allowed_brands=("리바로",),
        query_layer=BrokenLayer(),  # type: ignore[arg-type]
    )

    execution = facade.execute("get_metric", {"brand": "리바로", "measure": "sales", "period": "2025-04"})

    assert execution.status == "error"
    assert execution.call["tool"] == "query_failed"
    assert execution.call["render_data"]["status"] == "query_failed"
    assert "sales_억원" not in execution.call["render_data"]


def test_query_uses_the_source_that_contains_an_iqvia_only_fallback_brand() -> None:
    """Given no explicit source, a filtered query follows the fallback brand's available source."""

    periods = ("2026-Q1",)
    layer = StrategicQueryLayer(
        reader=StaticStrategicMartReader(
            (
                replace(_record_with_market("ml_003", "UBIST브랜드", (100.0,), periods, [100.0]), source="ubist"),
                replace(_record_with_market("ml_003", "마운자로", (200.0,), periods, [200.0]), source="iqvia_nsa"),
            )
        )
    )

    call = layer.query(
        {"market": "ml_003", "metrics": ["sales"], "filters": {"brand": "마운자로", "period": "latest"}},
        fallback_brand="마운자로",
    )

    assert call["source"] == "IQVIA"
    assert call["render_data"]["source_label"] == "IQVIA"


def test_iqvia_average_share_converts_six_months_to_two_quarters() -> None:
    periods = ("2025-Q1", "2025-Q2", "2025-Q3", "2025-Q4", "2026-Q1")
    layer = StrategicQueryLayer(
        reader=StaticStrategicMartReader(
            (
                replace(
                    _record_with_market("ml_003", "마운자로", (100.0, 120.0, 140.0, 160.0, 200.0), periods, [100.0, 120.0, 140.0, 160.0, 200.0]),
                    source="iqvia_nsa",
                ),
            )
        )
    )

    call = layer.query(
        {
            "market": "ml_003",
            "source": "iqvia_nsa",
            "metrics": ["share"],
            "derive": ["average"],
            "filters": {"brand": "마운자로", "periods": "6"},
        },
        fallback_brand="마운자로",
    )

    data = call["render_data"]
    assert [row["period"] for row in data["brand_value_series_10pt"]] == ["2025-Q4", "2026-Q1"]
    assert data["requested_window_months"] == 6
    assert data["observation_count"] == 2
    assert data["window_grain"] == "quarter"


def test_average_share_plan_leaves_source_to_brand_availability() -> None:
    plan = strict_query_plan("마운자로의 최근 6개월 시장점유율 평균은?", "마운자로")

    assert plan is not None
    assert plan.specs[0]["source"] == ""


def test_chat_agent_simple_split_metric_uses_query_layer_structure() -> None:
    """Given a split-market brand, simple metric routing still carries the registry basis."""

    agent = ChatAgent(
        metrics=_metrics_tool(),
        resolver=BrandResolver(),
        query_layer=StrategicQueryLayer(reader=StaticStrategicMartReader(_split_class_records())),
    )

    result = agent.answer("악템라 매출 추이 알려줘")

    assert result["tool_calls"][0]["render_data"]["market_structure"]["type"] == "class_split"
    fact_md = result["markdown_response"]["fact_md"]
    assert "Class 구분 존재" in fact_md
    assert "Class 2 기준" in fact_md


def test_chat_agent_split_metric_period_filter_uses_query_layer_fallback() -> None:
    """Given a split-market period filter fails, the standard route still surfaces structure and fallback facts."""

    agent = ChatAgent(
        metrics=_metrics_tool(),
        resolver=BrandResolver(),
        query_layer=StrategicQueryLayer(
            reader=StaticStrategicMartReader(
                (
                    _split_record_with_status_history(
                        "악템라",
                        {
                            "2025-Q4": {"raw_value": 4_819_000_000.0, "ms": 4.34, "source_status": "OK"},
                            "2026-04": {"raw_value": 0.0, "ms": 0.0, "source_status": "query_failed"},
                        },
                        class_1="Biologic",
                        class_2="IL-6",
                    ),
                    _split_record_with_status_history(
                        "케브자라",
                        {
                            "2025-Q4": {"raw_value": 3_000_000_000.0, "ms": 2.70, "source_status": "OK"},
                            "2026-04": {"raw_value": 10_000_000_000.0, "ms": 100.0, "source_status": "OK"},
                        },
                        class_1="Biologic",
                        class_2="IL-6",
                    ),
                )
            )
        ),
    )

    result = agent.answer("악템라 2026-04 매출 알려줘")
    call = result["tool_calls"][0]
    data = call["render_data"]
    fact_md = result["markdown_response"]["fact_md"]

    assert call["source"] == "IQVIA"
    assert data["period"] == "2025-Q4"
    assert data["requested_period"] == "2026-04"
    assert data["fallback_period"] == "2025-Q4"
    assert data["market_structure"]["type"] == "class_split"
    assert "Class 구분 존재" in fact_md
    assert "Class 2 기준" in fact_md
    assert "사용 가능한 최신 기준" in fact_md
    assert fact_md.count("2026-04 값은 조회 실패/시장 매핑 불완전으로 표시하지 않습니다.") == 1
    assert "2026-04 매출 0.00억원" not in fact_md
    assert "2026-04 MS 0.00%" not in fact_md


def test_facade_prefers_query_layer_for_strategic_metric() -> None:
    """Given the query layer is available, get_metric returns mart-derived facts."""

    facade = AgentToolFacade(
        metrics=_metrics_tool(),
        resolver=BrandResolver(),
        allowed_brands=("리바로",),
        query_layer=_query_layer(),
    )

    execution = facade.execute("get_metric", {"brand": "리바로", "measure": "series", "period": "latest"})

    data = execution.call["render_data"]
    assert execution.call["source"] == "UBIST"
    assert data["brand"] == "리바로"
    assert data["period"] == "2026-04"
    assert data["sales_krw"] == 9_000_000_000.0
    assert data["query_result_id"].startswith("qr_")
    assert {row["brand"] for row in data["level_top5_trend_series"]} >= {"아토젯", "리바로"}
    livaro = next(row for row in data["level_top5_trend_series"] if row["brand"] == "리바로")
    assert livaro["from_period"] == livaro["series"][0]["period"]
    assert livaro["from_ms_pct"] == livaro["series"][0]["ms_pct"]
    assert livaro["to_period"] == livaro["series"][-1]["period"]
    assert livaro["to_ms_pct"] == livaro["series"][-1]["ms_pct"]


def test_query_layer_blocks_failed_latest_zero_metric() -> None:
    """Given the latest row failed upstream lookup, it is not surfaced as real zero."""

    layer = StrategicQueryLayer(
        reader=StaticStrategicMartReader(
            (
                _record_with_status_history(
                    "ml_011",
                    "악템라",
                    {
                        "2025-Q4": {"raw_value": 4_819_000_000.0, "ms": 4.34, "source_status": "OK"},
                        "2026-04": {"raw_value": 0.0, "ms": 0.0, "source_status": "mapping_failed"},
                    },
                    source="iqvia_nsa",
                ),
                _record_with_status_history(
                    "ml_011",
                    "경쟁품",
                    {
                        "2025-Q4": {"raw_value": 106_239_000_000.0, "ms": 95.66, "source_status": "OK"},
                        "2026-04": {"raw_value": 120_000_000_000.0, "ms": 100.0, "source_status": "OK"},
                    },
                    source="iqvia_nsa",
                ),
            )
        )
    )

    result = layer.brand_metric("악템라", "sales", "latest")
    data = result["render_data"]

    assert data["period"] == "2025-Q4"
    assert data["source_status"] == "OK"
    assert data["sales_억원"] == 48.19
    assert data["ms_recent_pct"] == 4.34
    assert "2026-04 값은 조회 실패" in data["blocked_metric_values"][0]["message"]
    assert "0.00억원" not in result["summary_text"]
    assert "MS 0.00%" not in result["summary_text"]


def test_query_layer_falls_back_from_failed_requested_period_and_keeps_split_structure() -> None:
    """Given a requested period failed, the valid prior period and market structure remain surfaceable."""

    layer = StrategicQueryLayer(
        reader=StaticStrategicMartReader(
            (
                _split_record_with_status_history(
                    "악템라",
                    {
                        "2025-Q3": {"raw_value": 5_823_000_000.0, "ms": 5.12, "source_status": "OK"},
                        "2025-Q4": {"raw_value": 4_819_000_000.0, "ms": 4.34, "source_status": "OK"},
                        "2026-04": {"raw_value": 0.0, "ms": 0.0, "source_status": "mapping_failed"},
                    },
                    class_1="Biologic",
                    class_2="IL-6",
                ),
                _split_record_with_status_history(
                    "경쟁품",
                    {
                        "2025-Q3": {"raw_value": 107_887_000_000.0, "ms": 94.88, "source_status": "OK"},
                        "2025-Q4": {"raw_value": 106_239_000_000.0, "ms": 95.66, "source_status": "OK"},
                        "2026-04": {"raw_value": 120_000_000_000.0, "ms": 100.0, "source_status": "OK"},
                    },
                    class_1="Biologic",
                    class_2="IL-6",
                ),
            )
        )
    )

    result = layer.brand_metric("악템라", "sales", "2026-04")
    data = result["render_data"]

    assert result["tool"] == "get_brand_metric"
    assert data["period"] == "2025-Q4"
    assert data["requested_period"] == "2026-04"
    assert data["source_status"] == "OK"
    assert data["sales_억원"] == 48.19
    assert data["ms_recent_pct"] == 4.34
    assert data["market_structure"]["type"] == "class_split"
    assert data["market_structure"]["display_axis"] == "class_2"
    assert data["blocked_metric_values"] == [
        {
            "period": "2026-04",
            "status": "mapping_failed",
            "message": "2026-04 값은 조회 실패/시장 매핑 불완전으로 표시하지 않습니다.",
        }
    ]
    series_periods = [item["period"] for item in data["brand_value_series_10pt"]]
    assert series_periods == ["2025-Q3", "2025-Q4"]
    assert "0.00억원" not in result["summary_text"]
    assert "MS 0.00%" not in result["summary_text"]
    fact_md = answer_fact_markdown([result], [result["source"]])
    assert "Class 구분 존재" in fact_md
    assert "Class 2 기준" in fact_md
    assert "사용 가능한 최신 기준" in fact_md
    assert "2025-Q4" in fact_md
    assert fact_md.count("2026-04 값은 조회 실패/시장 매핑 불완전으로 표시하지 않습니다.") == 1
    assert "2026-04 매출 0.00억원" not in fact_md
    assert "2026-04 MS 0.00%" not in fact_md


def test_query_layer_keeps_status_ok_true_zero_metric() -> None:
    """Given a row is explicitly successful, a raw zero remains displayable."""

    layer = StrategicQueryLayer(
        reader=StaticStrategicMartReader(
            (
                _record_with_status_history(
                    "ml_zero",
                    "제로브랜드",
                    {"2026-04": {"raw_value": 0.0, "ms": 0.0, "source_status": "OK"}},
                ),
                _record_with_status_history(
                    "ml_zero",
                    "비교브랜드",
                    {"2026-04": {"raw_value": 100_000_000.0, "ms": 100.0, "source_status": "OK"}},
                ),
            )
        )
    )

    result = layer.brand_metric("제로브랜드", "sales", "latest")
    data = result["render_data"]

    assert data["period"] == "2026-04"
    assert data["source_status"] == "OK"
    assert data["sales_억원"] == 0.0
    assert data["ms_recent_pct"] == 0.0
    assert data["rank"] == 2
    assert "0.00억원" in result["summary_text"]


def test_patent_facade_adds_market_based_competitor_ingredient_candidates() -> None:
    facade = AgentToolFacade(
        metrics=_metrics_tool(),
        resolver=BrandResolver(),
        allowed_brands=("리바로",),
        query_layer=_query_layer(),
        external=PatentEchoExternal(),
    )

    execution = facade.execute("search_patent", {"brand": "리바로", "query": "경쟁 성분의 특허/독점권"})

    data = execution.call["render_data"]
    candidates = data["competitor_ingredient_candidates"]
    assert execution.status == "ok"
    assert [candidate["molecule"] for candidate in candidates[:3]] == ["로수젯성분", "리피토성분", "아토젯성분"]
    assert all(candidate["source"] == "UBIST" for candidate in candidates)
    assert all(candidate["market"] == "ml_006" for candidate in candidates)
    assert data["competitor_patent_coverage"]["status"] == "attempted"
    queried = [call["render_data"]["query"] for call in data["calls"] if call["tool"] == "mfds_patent"]
    assert {"pitavastatin", "로수젯성분", "리피토성분", "아토젯성분"}.issubset(set(queried))


def test_market_member_metric_reads_comparison_brand_series() -> None:
    """Given a market-member brand, query layer can return comparison series without resolver support."""

    call = _query_layer().market_member_metric("리바로", "아토젯")

    data = call["render_data"]
    assert call["source"] == "UBIST"
    assert data["brand"] == "아토젯"
    assert data["metric"] == "market_member_series"
    assert data["ms_recent_pct"] == pytest.approx(13_000_000_000.0 / 81_800_000_000.0 * 100)
    assert len(data["brand_value_series_10pt"]) == 4


def test_query_spec_rejects_absent_market_dimension() -> None:
    """Given an absent dimension, query(spec) fails schema validation."""

    with pytest.raises(ValueError, match="dimensions unknown"):
        _query_layer().query({"market": "ml_006", "source": "ubist", "dimensions": ["nhi_type"], "metrics": ["sales"]}, fallback_brand="리바로")


def test_query_spec_applies_channel_filter_to_molecule_population() -> None:
    """Given a channel filter, query(spec) returns molecule shares from that channel population."""

    call = _query_layer().query(
        {
            "market": "ml_006",
            "source": "ubist",
            "dimensions": ["molecule"],
            "group_by": ["molecule"],
            "metrics": ["share"],
            "filters": {"channel": "의원"},
        },
        fallback_brand="리바로",
    )

    data = call["render_data"]
    rows = {row["name"]: row for row in data["level_segments"]}
    assert data["applied_filters"] == {"channel": "의원"}
    assert rows["피타바스타틴"]["ms_recent_pct"] == pytest.approx(4_500_000_000 / 22_700_000_000 * 100)
    assert rows["아토젯성분"]["ms_recent_pct"] == pytest.approx(3_250_000_000 / 22_700_000_000 * 100)


def test_query_spec_groups_channel_and_specialty_nested_populations() -> None:
    """Given nested mart dimensions, query(spec) groups their latest-period values."""

    channel_call = _query_layer().query(
        {"market": "ml_006", "source": "ubist", "dimensions": ["channel"], "group_by": ["channel"], "metrics": ["share"]},
        fallback_brand="리바로",
    )
    specialty_call = _query_layer().query(
        {"market": "ml_006", "source": "ubist", "dimensions": ["specialty"], "group_by": ["specialty"], "metrics": ["sales"]},
        fallback_brand="리바로",
    )

    channels = {row["name"]: row for row in channel_call["render_data"]["level_segments"]}
    specialties = {row["name"]: row for row in specialty_call["render_data"]["level_segments"]}
    assert channels["의원"]["value"] == pytest.approx(22_700_000_000)
    assert channels["종병"]["value"] == pytest.approx(31_820_000_000)
    assert specialties["순환기"]["value"] == pytest.approx(49_080_000_000)


def test_query_spec_builds_dimension_trend_for_period_grouping() -> None:
    """Given period grouping, query(spec) exposes fact-backed series for top dimension groups."""

    call = _query_layer().query(
        {
            "market": "ml_006",
            "source": "ubist",
            "dimensions": ["dosage_form"],
            "group_by": ["dosage_form", "period"],
            "metrics": ["sales"],
            "derive": ["trend"],
            "filters": {"periods": "3"},
            "limit": 5,
        },
        fallback_brand="리바로",
    )

    data = call["render_data"]
    trend = data["level_top5_trend_series"]
    assert data["level"] == "dosage_form"
    assert {row["brand"] for row in trend} >= {"정제", "복합정"}
    first = next(row for row in trend if row["brand"] == "정제")
    assert [point["period"] for point in first["series"]] == ["2026-02", "2026-03", "2026-04"]
    assert first["from_ms_pct"] == first["series"][0]["ms_pct"]
    assert first["to_ms_pct"] == first["series"][-1]["ms_pct"]


def test_query_spec_groups_class2_for_split_market() -> None:
    """Given a split market, query(spec) can group the exposed Class 2 population."""

    layer = StrategicQueryLayer(reader=StaticStrategicMartReader(_split_class_records()))

    call = layer.query(
        {
            "market": "ml_011",
            "source": "iqvia_nsa",
            "dimensions": ["class_2"],
            "group_by": ["class_2"],
            "metrics": ["sales"],
        },
        fallback_brand="악템라",
    )

    data = call["render_data"]
    rows = {row["name"]: row for row in data["level_segments"]}
    assert data["level"] == "class_2"
    assert data["market_structure"]["type"] == "class_split"
    assert data["market_structure"]["display_axis"] == "class_2"
    assert rows["IL-6"]["value"] == pytest.approx(7_000_000_000.0)
    assert rows["JAK"]["value"] == pytest.approx(3_000_000_000.0)


def test_query_spec_calculates_yoy_growth_and_average_share() -> None:
    """Given derive specs, query(spec) returns deterministic YoY and average facts."""

    yoy_call = _query_layer().query(
        {
            "market": "ml_006",
            "source": "ubist",
            "dimensions": ["product"],
            "group_by": ["product"],
            "metrics": ["growth"],
            "derive": ["yoy"],
            "filters": {"brand": "리바로"},
        },
        fallback_brand="리바로",
    )
    avg_call = _query_layer().query(
        {
            "market": "ml_006",
            "source": "ubist",
            "dimensions": ["product"],
            "group_by": ["product"],
            "metrics": ["share"],
            "derive": ["average"],
            "filters": {"brand": "리바로", "periods": "6"},
        },
        fallback_brand="리바로",
    )

    yoy_data = yoy_call["render_data"]
    avg_data = avg_call["render_data"]
    assert yoy_data["brand"] == "리바로"
    assert yoy_data["metric"] == "yoy_growth"
    assert yoy_data["growth_pct"] == pytest.approx((9_000_000_000 / 7_000_000_000 - 1) * 100, abs=0.0001)
    assert avg_data["metric"] == "average_share"
    assert avg_data["avg_ms_pct"] == pytest.approx(sum(point["ms_pct"] for point in avg_data["brand_value_series_10pt"]) / 4, abs=0.0001)


def test_completion_uses_query_layer_for_unsupported_comparison_brand() -> None:
    """Given 아토젯 is not in resolver catalog, completion recovers it from mart series."""

    planner = ScriptedPlanner(
        (
            AgentDecision(
                tool_calls=(ToolCallPlan(name="get_metric", arguments={"brand": "리바로", "measure": "series", "period": "latest"}, reason="anchor series"),)
            ),
            AgentDecision(final_answer="done"),
        )
    )
    agent = ToolUseAgent(metrics=_metrics_tool(), resolver=BrandResolver(), planner=planner, query_layer=_query_layer())

    result = agent.answer("리바로 뉴스에서 아토젯 이슈랑 매출 변화 같이 봐줘")

    comparison_calls = [
        call
        for call in result["tool_calls"]
        if call.get("tool") == "get_brand_metric" and call.get("render_data", {}).get("brand") == "아토젯"
    ]
    assert comparison_calls
    assert comparison_calls[0]["source"] == "UBIST"
    assert comparison_calls[0]["render_data"]["metric"] == "market_member_series"


def test_concentration_backfill_uses_observed_brand_for_market_anaphora() -> None:
    planner = ScriptedPlanner(
        (
            AgentDecision(
                tool_calls=(
                    ToolCallPlan(
                        name="get_metric",
                        arguments={"brand": "리바로", "measure": "sales", "period": "latest"},
                        reason="이전 시장 anchor의 브랜드 지표",
                    ),
                )
            ),
            AgentDecision(final_answer="done"),
        )
    )
    agent = ToolUseAgent(metrics=_metrics_tool(), resolver=BrandResolver(), planner=planner, query_layer=_query_layer())

    result = agent.answer("이 시장 집중도는 어때?")

    market_scope = next(call for call in result["tool_calls"] if call.get("tool") == "get_market_landscape")
    assert market_scope["render_data"]["anchor_brand"] == "리바로"
    assert market_scope["render_data"]["hhi_recent"] is not None


def test_share_comparison_completion_uses_query_layer_for_market_member() -> None:
    """Given a share trend comparison, unsupported comparison terms are completed from mart."""

    planner = ScriptedPlanner(
        (
            AgentDecision(
                tool_calls=(ToolCallPlan(name="get_metric", arguments={"brand": "리바로", "measure": "series", "period": "latest"}, reason="anchor series"),)
            ),
            AgentDecision(final_answer="done"),
        )
    )
    agent = ToolUseAgent(metrics=_metrics_tool(), resolver=BrandResolver(), planner=planner, query_layer=_query_layer())

    result = agent.answer("리바로와 아토젯의 점유율 변화 비교")

    brands = {
        call.get("render_data", {}).get("brand")
        for call in result["tool_calls"]
        if call.get("tool") == "get_brand_metric"
    }
    assert {"리바로", "아토젯"}.issubset(brands)


def test_threat_question_adds_pair_trend_comparison_fact() -> None:
    """Given a threat question, agent computes a two-brand trend comparison fact."""

    planner = ScriptedPlanner(
        (
            AgentDecision(
                tool_calls=(ToolCallPlan(name="get_metric", arguments={"brand": "리바로", "measure": "series", "period": "latest"}, reason="anchor series"),)
            ),
            AgentDecision(final_answer="done"),
        )
    )
    agent = ToolUseAgent(metrics=_metrics_tool(), resolver=BrandResolver(), planner=planner, query_layer=_query_layer())

    result = agent.answer("아토젯이 리바로를 위협하고 있어?")

    calculations = [
        call
        for call in result["tool_calls"]
        if call.get("tool") == "agent_calculation" and call.get("render_data", {}).get("metric") == "brand_trend_comparison"
    ]
    assert calculations
    comparison = calculations[0]["render_data"]
    assert comparison["brand"] == "리바로"
    assert comparison["comparison_brand"] == "아토젯"
    assert comparison["brand_share_delta_pctp"] == pytest.approx(
        result["tool_calls"][0]["render_data"]["level_top5_trend_series"][-1]["share_delta_pctp"]
    )
    assert "브랜드 추세 비교" in result["markdown_response"]["fact_md"]
    assert "아토젯" in result["markdown_response"]["fact_md"]


def test_population_question_forces_query_spec_instead_of_generic_metric() -> None:
    """Given a channel dimension question, agent adds a strict query(spec) population call."""

    planner = ScriptedPlanner(
        (
            AgentDecision(tool_calls=(ToolCallPlan(name="get_metric", arguments={"brand": "리바로", "measure": "sales", "period": "latest"}, reason="generic metric"),)),
            AgentDecision(final_answer="done"),
        )
    )
    agent = ToolUseAgent(metrics=_metrics_tool(), resolver=BrandResolver(), planner=planner, query_layer=_query_layer())

    result = agent.answer("리바로 의원 채널에서 성분별 점유율")

    query_calls = [
        call
        for call in result["tool_calls"]
        if call.get("tool") == "get_brand_metric" and call.get("render_data", {}).get("metric") == "query_spec"
    ]
    assert query_calls
    data = query_calls[0]["render_data"]
    assert data["level"] == "Molecule"
    assert data["applied_filters"] == {"channel": "의원"}


def test_segment_compare_collects_axis_facts_instead_of_unsupported_only() -> None:
    """Given a segment comparison question, supported axes are queried and absent axes remain axis-scoped."""

    planner = ScriptedPlanner(
        (
            AgentDecision(tool_calls=(ToolCallPlan(name="get_metric", arguments={"brand": "리바로", "measure": "sales", "period": "latest"}, reason="generic metric"),)),
            AgentDecision(final_answer="done"),
        )
    )
    agent = ToolUseAgent(metrics=_metrics_tool(), resolver=BrandResolver(), planner=planner, query_layer=_query_layer())

    result = agent.answer("리바로 처방을 Class/Molecule/브랜드/용량/제형 세그먼트별로 비교해줘")

    tools = [call.get("tool") for call in result["tool_calls"]]
    assert tools != ["unsupported_metric"]
    query_calls = [
        call
        for call in result["tool_calls"]
        if call.get("tool") == "get_brand_metric" and call.get("render_data", {}).get("metric") == "query_spec"
    ]
    axes = {call["render_data"].get("requested_axis") for call in query_calls}
    assert {"Molecule", "브랜드", "제형"}.issubset(axes)
    failed_axes = {
        call.get("render_data", {}).get("requested_axis")
        for call in result["tool_calls"]
        if call.get("tool") == "query_failed"
    }
    assert {"Class", "용량"}.issubset(failed_axes)
    fact_md = result["markdown_response"]["fact_md"]
    assert "Molecule 지원" in fact_md
    assert "브랜드 지원" in fact_md
    assert "제형 지원" in fact_md
    assert "Class 미지원" in fact_md
    assert "용량 미지원" in fact_md


def test_source_crosscheck_collects_ubist_and_iqvia_separately() -> None:
    """Given a source cross-check question, available sources surface without pretending a cross-check happened."""

    planner = ScriptedPlanner(
        (
            AgentDecision(tool_calls=(ToolCallPlan(name="get_metric", arguments={"brand": "리바로", "measure": "sales", "period": "latest"}, reason="generic metric"),)),
            AgentDecision(final_answer="done"),
        )
    )
    agent = ToolUseAgent(metrics=_metrics_tool(), resolver=BrandResolver(), planner=planner, query_layer=_query_layer())

    result = agent.answer("리바로 UBIST와 IQVIA 데이터 출처별로 교차 확인해줘")

    sources = {
        call.get("render_data", {}).get("requested_source"): call
        for call in result["tool_calls"]
        if call.get("render_data", {}).get("contract_intent") == "source_crosscheck"
    }
    assert sources["UBIST"]["tool"] == "get_brand_metric"
    assert sources["IQVIA"]["tool"] == "query_failed"
    assert sources["UBIST"]["render_data"]["requested_brand"] == "리바로"
    assert sources["UBIST"]["render_data"].get("filters", {}).get("brand") is None
    fact_md = result["markdown_response"]["fact_md"]
    assert "UBIST 보유" in fact_md
    assert "IQVIA 미보유" in fact_md
    assert "100.00%→100.00%" not in fact_md
    assert "교차 판정" not in fact_md


@pytest.mark.parametrize(
    "question",
    [
        "리바로 채널별로 보여줘",
        "리바로 채널",
        "리바로 어느 채널에서 잘 팔려?",
        "리바로 의원/병원별 실적",
        "리바로 채널별 매출",
        "리바로 유통 채널",
        "리바로 채널 구성",
        "리바로 채널 분포",
        "리바로 채널 mix",
    ],
)
def test_generic_channel_question_maps_to_channel_distribution_query(question: str) -> None:
    planner = ScriptedPlanner((AgentDecision(final_answer="done"),))
    agent = ToolUseAgent(metrics=_metrics_tool(), resolver=BrandResolver(), planner=planner, query_layer=_query_layer())

    result = agent.answer(question)

    query_calls = [
        call
        for call in result["tool_calls"]
        if call.get("tool") == "get_brand_metric" and call.get("render_data", {}).get("metric") == "query_spec"
    ]
    assert query_calls
    data = query_calls[0]["render_data"]
    rows = {row["name"]: row for row in data["level_segments"]}
    assert data["level"] == "channel"
    assert data["applied_filters"] == {"brand": "리바로"}
    assert rows["의원"]["value"] == pytest.approx(4_500_000_000.0)
    assert rows["종병"]["value"] == pytest.approx(2_700_000_000.0)


def test_channel_sales_question_keeps_channel_facts_visible() -> None:
    planner = ScriptedPlanner((AgentDecision(final_answer="done"),))
    agent = ToolUseAgent(metrics=_metrics_tool(), resolver=BrandResolver(), planner=planner, query_layer=_query_layer())

    result = agent.answer("리바로 채널별 매출")

    query_call = next(
        call
        for call in result["tool_calls"]
        if call.get("tool") == "get_brand_metric" and call.get("render_data", {}).get("metric") == "query_spec"
    )
    data = query_call["render_data"]
    fact_md = result["markdown_response"]["fact_md"]
    assert data["level"] == "channel"
    assert data.get("answer_scope") is None
    assert "### 필수 답변 fact" in fact_md
    assert "channel 상위" in fact_md
    assert "### 리바로 channel별 점유율 fact" in fact_md
    assert "의원" in fact_md
    assert "45.00억원" in fact_md


def test_channel_share_question_keeps_share_metric() -> None:
    plan = strict_query_plan("리바로와 아토젯 채널별 점유율", "리바로")

    assert plan is not None
    assert [spec["metrics"] for spec in plan.specs] == [["share"], ["share"]]
    assert [spec["filters"] for spec in plan.specs] == [{"brand": "리바로"}, {"brand": "아토젯"}]


@pytest.mark.parametrize(
    "question",
    [
        "리바로 채널별로 보여줘",
        "리바로 채널",
        "리바로 의원/병원별 실적",
        "리바로 유통 채널",
    ],
)
def test_channel_paraphrases_enter_agent_loop(question: str) -> None:
    assert should_use_agent_loop(question)


@pytest.mark.parametrize(
    "question",
    [
        "리바로 채널 파트너",
        "리바로 유튜브 채널",
    ],
)
def test_non_analytic_channel_phrases_do_not_enter_agent_loop(question: str) -> None:
    assert not should_use_agent_loop(question)


def test_absent_dimension_question_blocks_generic_cache_fallback() -> None:
    """Given a missing dimension request, agent returns unsupported only for that dimension intent."""

    planner = ScriptedPlanner(
        (
            AgentDecision(tool_calls=(ToolCallPlan(name="get_metric", arguments={"brand": "리바로", "measure": "series", "period": "latest"}, reason="generic trend"),)),
            AgentDecision(final_answer="done"),
        )
    )
    agent = ToolUseAgent(metrics=_metrics_tool(), resolver=BrandResolver(), planner=planner, query_layer=_query_layer())

    result = agent.answer("리바로 급여/비급여 매출 구성과 추이")

    assert [call.get("tool") for call in result["tool_calls"]] == ["unsupported_metric"]
    assert "nhi_type" in result["tool_calls"][0]["render_data"]["message"]


def test_query_execution_failure_is_not_reported_as_unsupported(caplog) -> None:
    """Given query execution fails, the facade reports 조회 실패 instead of 데이터 미보유."""

    facade = AgentToolFacade(
        metrics=_metrics_tool(),
        resolver=BrandResolver(),
        allowed_brands=("리바로",),
        query_layer=None,
    )

    execution = facade.execute(
        "query",
        {
            "brand": "리바로",
            "spec": '{"market":"ml_006","source":"ubist","dimensions":["channel"],"metrics":["sales"]}',
        },
    )

    data = execution.call["render_data"]
    assert execution.status == "error"
    assert execution.call["tool"] == "query_failed"
    assert data["status"] == "query_failed"
    assert data["error_type"] == "LookupError"
    assert "조회 실행이 실패" in data["message"]
    assert "데이터가 없다는 뜻" in data["message"]
    assert any(record.message == "agent_tool_execution_failed" for record in caplog.records)


def test_query_failure_preserves_split_market_structure_metadata(caplog) -> None:
    """Given a split-market query fails, the error call still carries class structure metadata."""

    facade = AgentToolFacade(
        metrics=_metrics_tool(),
        resolver=BrandResolver(),
        allowed_brands=("악템라",),
        query_layer=StrategicQueryLayer(reader=StaticStrategicMartReader(_split_class_records())),
    )

    execution = facade.execute(
        "query",
        {
            "brand": "악템라",
            "spec": '{"market":"ml_011","source":"iqvia_nsa","dimensions":["class"],"metrics":["sales"]}',
        },
    )

    data = execution.call["render_data"]
    assert execution.call["tool"] == "query_failed"
    assert data["status"] == "query_failed"
    assert data["market_id"] == "ml_011"
    assert data["market_structure"]["type"] == "class_split"
    assert data["market_structure"]["display_axis"] == "class_2"
    fact_md = answer_fact_markdown([execution.call], ["IQVIA NSA"])
    assert "Class 구조 기준" in fact_md
    assert "Class 2 기준" in fact_md
    assert any(record.message == "agent_tool_execution_failed" for record in caplog.records)


def test_unsupported_brand_still_reports_unsupported_metric() -> None:
    """Given the brand is outside the allowed enum, the facade keeps the unsupported taxonomy."""

    facade = AgentToolFacade(
        metrics=_metrics_tool(),
        resolver=BrandResolver(),
        allowed_brands=("리바로",),
        query_layer=_query_layer(),
    )

    execution = facade.execute("get_brand_sales", {"brand": "없는브랜드", "period": "latest"})

    data = execution.call["render_data"]
    assert execution.status == "error"
    assert execution.call["tool"] == "unsupported_metric"
    assert data["status"] == "unsupported"
    assert "지원" in data["message"] or "allowed canonical brand" in data["message"]


def test_top_brand_questions_enter_agent_loop() -> None:
    """Given a top-brands question, routing should use query-capable agent loop."""

    assert should_use_agent_loop("리바로 시장에서 상위 브랜드 뭐 있어")


def test_portfolio_decline_question_uses_deterministic_decline_analysis() -> None:
    """Given a company-scope decline question, the agent analyzes the portfolio before LLM planning."""

    resolver = BrandResolver(
        mode="cache",
        brand_reader=StaticMetricsCacheReader(cache_brands=CACHE_BRANDS, market_status=BRAND_CARDS),
    )
    planner = ScriptedPlanner((AgentDecision(final_answer="planner should not run"),))
    agent = ToolUseAgent(
        metrics=_metrics_tool(),
        resolver=resolver,
        planner=planner,
        query_layer=_portfolio_query_layer(),
    )

    result = agent.answer("JW 주요 브랜드 중 최근 시장점유율이 하락한 게 있으면 어떤 브랜드인지, 그 시장에서 누가 점유율을 가져갔는지 원인을 분석해줘")

    assert result["resolution"] == {"canonical_brand": "JW 주요 브랜드", "scope": "portfolio"}
    assert result["agent_trace"] == []
    assert result["tool_calls"][0]["tool"] == "portfolio_decline_analysis"
    data = result["tool_calls"][0]["render_data"]
    decliners = {row["brand"]: row for row in data["decliners"]}
    assert "페린젝트" in decliners
    assert decliners["페린젝트"]["share_delta_pctp"] < 0
    assert decliners["페린젝트"]["top_gainers"][0]["brand"] == "베노훼럼"
    assert "JW 주요 브랜드 포트폴리오 fact" in result["markdown_response"]["fact_md"]
    assert "직접 인과/처방 이동 단정 불가" in result["markdown_response"]["fact_md"]
    assert "베노훼럼" in result["answer"]


@pytest.mark.parametrize(
    "question",
    [
        "JW 주요 브랜드 중 하락한 거 원인 분석",
        "JW 주요 브랜드 중 떨어진 브랜드 원인 분석",
        "부진한 자사 제품 알려줘",
        "우리 제품 중 밀리는 거 원인 분석",
    ],
)
def test_portfolio_decline_variants_use_deterministic_analysis(question: str) -> None:
    """Given a short company-scope variant, the agent must not require one brand."""

    resolver = BrandResolver(
        mode="cache",
        brand_reader=StaticMetricsCacheReader(cache_brands=CACHE_BRANDS, market_status=BRAND_CARDS),
    )
    planner = ScriptedPlanner((AgentDecision(final_answer="planner should not run"),))
    agent = ToolUseAgent(
        metrics=_metrics_tool(),
        resolver=resolver,
        planner=planner,
        query_layer=_portfolio_query_layer(),
    )

    result = agent.answer(question)

    assert result["resolution"] == {"canonical_brand": "JW 주요 브랜드", "scope": "portfolio"}
    assert result["agent_trace"] == []
    assert result["tool_calls"][0]["tool"] == "portfolio_decline_analysis"
    assert "직접 인과" in result["answer"]
    assert "단정" in result["answer"]


@pytest.mark.parametrize(
    "question",
    [
        "리바로와 아토젯의 채널별 점유율 차이",
        "리바로 시장 오리지널 vs 제네릭 비중",
        "리바로 제형별 매출 추이(최근 1년)",
        "리바로 시장에서 급매출 회사 top3와 그 성분",
        "리바로 급여/비급여 매출 구성과 추이",
        "리바로의 지난 6개월 평균 점유율은?",
    ],
)
def test_population_sensitive_questions_enter_agent_loop(question: str) -> None:
    """Given dimension or aggregation intents, routing must not use generic cache metrics."""

    assert should_use_agent_loop(question)


def test_simple_sales_question_stays_single_shot() -> None:
    """Given a simple metric request, fallback blocking should not break single-shot metrics."""

    assert not should_use_agent_loop("리바로 매출")


def _query_layer() -> StrategicQueryLayer:
    return StrategicQueryLayer(reader=StaticStrategicMartReader(_records()))


def test_market_scope_includes_latest_hhi_for_concentration_answers() -> None:
    call = _query_layer().market_scope("리바로")

    assert call["render_data"]["hhi_recent"] > 0
    assert call["render_data"]["period"] == "2026-04"


def _portfolio_query_layer() -> StrategicQueryLayer:
    return StrategicQueryLayer(reader=StaticStrategicMartReader(_portfolio_records()))


def _metrics_tool() -> MetricsTool:
    return MetricsTool(
        mode="cache",
        cache_reader=StaticMetricsCacheReader(cache_brands=CACHE_BRANDS, market_status=BRAND_CARDS),
        cause_reader=StaticCausePayloadReader({}),
    )


def _records() -> tuple[MartRecord, ...]:
    periods = ("2025-04", "2026-02", "2026-03", "2026-04")
    values = {
        "로수젯": (19_000_000_000.0, 20_000_000_000.0, 21_000_000_000.0, 22_000_000_000.0),
        "리피토": (14_000_000_000.0, 15_000_000_000.0, 14_000_000_000.0, 14_500_000_000.0),
        "아토젯": (10_000_000_000.0, 11_000_000_000.0, 12_000_000_000.0, 13_000_000_000.0),
        "리바로젯": (11_500_000_000.0, 12_000_000_000.0, 12_500_000_000.0, 12_800_000_000.0),
        "로수바미브": (9_500_000_000.0, 10_000_000_000.0, 10_200_000_000.0, 10_500_000_000.0),
        "리바로": (7_000_000_000.0, 8_000_000_000.0, 8_500_000_000.0, 9_000_000_000.0),
    }
    totals = [sum(series[index] for series in values.values()) for index in range(len(periods))]
    return tuple(_record(brand, series, periods, totals) for brand, series in values.items())


def _portfolio_records() -> tuple[MartRecord, ...]:
    periods = ("2026-01", "2026-02", "2026-03", "2026-04")
    values = {
        "페린젝트": (4_000_000_000.0, 3_800_000_000.0, 3_500_000_000.0, 3_200_000_000.0),
        "베노훼럼": (2_000_000_000.0, 2_300_000_000.0, 2_600_000_000.0, 3_100_000_000.0),
        "경쟁철분": (4_000_000_000.0, 3_900_000_000.0, 3_900_000_000.0, 3_700_000_000.0),
    }
    totals = [sum(series[index] for series in values.values()) for index in range(len(periods))]
    return tuple(_record_with_market("ml_012", brand, series, periods, totals) for brand, series in values.items())


def _split_class_records() -> tuple[MartRecord, ...]:
    periods = ("2025-Q4",)
    values = {
        "악템라": (4_000_000_000.0, "Biologic", "IL-6"),
        "케브자라": (3_000_000_000.0, "Biologic", "IL-6"),
        "올루미언트": (2_000_000_000.0, "JAK", "JAK"),
        "린버크": (1_000_000_000.0, "JAK", "JAK"),
    }
    total = sum(float(item[0]) for item in values.values())
    return tuple(
        _split_record(brand, float(value), periods[0], total, class_1, class_2)
        for brand, (value, class_1, class_2) in values.items()
    )


def _split_market_ubist_records_without_class() -> tuple[MartRecord, ...]:
    periods = ("2025-12",)
    values = {
        "악템라": (4_200_000_000.0,),
        "케브자라": (3_100_000_000.0,),
    }
    total = [sum(series[0] for series in values.values())]
    return tuple(_record_with_market("ml_011", brand, series, periods, total) for brand, series in values.items())


def _split_record(brand: str, value: float, period: str, total: float, class_1: str, class_2: str) -> MartRecord:
    history = {period: {"raw_value": value, "ms": value / total * 100, "source_status": "OK"}}
    return MartRecord(
        ml_id="ml_011",
        brand_name=brand,
        source="iqvia_nsa",
        measure="sales",
        metric_history=history,
        channel_data={},
        specialty_data={},
        dimension_data={"class_1": {class_1: history}, "class_2": {class_2: history}},
        by_dimension={
            "company": "테스트제약",
            "molecule": f"{brand}성분",
            "class_1": class_1,
            "class_2": class_2,
        },
    )


def _split_record_with_status_history(
    brand: str,
    history: dict[str, dict[str, Any]],
    *,
    class_1: str,
    class_2: str,
) -> MartRecord:
    return MartRecord(
        ml_id="ml_011",
        brand_name=brand,
        source="iqvia_nsa",
        measure="sales",
        metric_history=history,
        channel_data={},
        specialty_data={},
        dimension_data={"class_1": {class_1: history}, "class_2": {class_2: history}},
        by_dimension={
            "company": "테스트제약",
            "molecule": f"{brand}성분",
            "class_1": class_1,
            "class_2": class_2,
        },
    )


def _record(brand: str, values: tuple[float, ...], periods: tuple[str, ...], totals: list[float]) -> MartRecord:
    return _record_with_market("ml_006", brand, values, periods, totals)


def _record_with_market(ml_id: str, brand: str, values: tuple[float, ...], periods: tuple[str, ...], totals: list[float]) -> MartRecord:
    history = {
        period: {"raw_value": values[index], "ms": values[index] / totals[index] * 100}
        for index, period in enumerate(periods)
    }
    channel_data = {
        "의원": _scaled_history(history, 0.50 if brand == "리바로" else 0.25),
        "종병": _scaled_history(history, 0.30 if brand == "리바로" else 0.40),
    }
    specialty_data = {
        "순환기": _scaled_history(history, 0.60),
        "내분비": _scaled_history(history, 0.25),
    }
    company = "JW중외제약" if brand in {"리바로", "리바로젯"} else "경쟁제약"
    molecule = "피타바스타틴" if brand == "리바로" else f"{brand}성분"
    dosage_form = "정제" if brand in {"리바로", "리피토"} else "복합정"
    ox_gx = "Original" if brand in {"리바로", "리피토"} else "Generic"
    return MartRecord(
        ml_id=ml_id,
        brand_name=brand,
        source="ubist",
        measure="sales",
        metric_history=history,
        channel_data=channel_data,
        specialty_data=specialty_data,
        dimension_data={"company": {company: history}, "ox_gx": {ox_gx: history}, "dosage_form": {dosage_form: history}},
        by_dimension={"company": company, "molecule": molecule, "ox_gx": ox_gx, "dosage_form": dosage_form, "class": dosage_form},
    )


def _record_with_status_history(
    ml_id: str,
    brand: str,
    history: dict[str, dict[str, Any]],
    *,
    source: str = "ubist",
) -> MartRecord:
    return MartRecord(
        ml_id=ml_id,
        brand_name=brand,
        source=source,
        measure="sales",
        metric_history=history,
        channel_data={},
        specialty_data={},
        dimension_data={},
        by_dimension={"company": "테스트제약", "molecule": f"{brand}성분", "dosage_form": "테스트"},
    )


def _scaled_history(history: dict[str, dict[str, float]], ratio: float) -> dict[str, dict[str, float]]:
    return {period: {"raw_value": row["raw_value"] * ratio} for period, row in history.items()}
