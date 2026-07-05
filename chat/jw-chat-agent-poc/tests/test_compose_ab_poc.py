from __future__ import annotations

from scripts.compose_ab_poc.analyses import execute_intent, primitive_trace, query_spec_trace
from scripts.compose_ab_poc.catalog import CompositionCatalog
from scripts.compose_ab_poc.grounding import ground_plan
from scripts.compose_ab_poc.mart_store import BrandRecord, MartStore


def test_target_share_gap_uses_market_total() -> None:
    """The 4% target gap must be computed from market value, not hallucinated."""

    store = _store()

    result = execute_intent(store, "target_share_gap")

    assert result.status == "ok"
    assert result.facts["market_value"] == 300.0
    assert result.facts["target_value"] == 12.0
    assert result.facts["needed_increase"] == 2.0


def test_channel_molecule_share_groups_across_market() -> None:
    """Clinic-channel molecule share is a market aggregation by molecule."""

    store = _store()

    result = execute_intent(store, "clinic_channel_molecule_share")

    rows = result.facts["rows"]
    assert rows[0]["molecule"] == "PTV"
    assert round(rows[0]["share_pct"], 2) == 83.33
    assert rows[1]["molecule"] == "ATV/EZE"


def test_trace_shapes_differ_between_approaches() -> None:
    """Primitive traces expose multiple steps while query-spec is compact."""

    store = _store()
    result = execute_intent(store, "top5_share_sum")

    primitive = primitive_trace("top5_share_sum", result)
    query = query_spec_trace("top5_share_sum", result)

    assert [step.tool for step in primitive] == ["fetch", "filter", "group_by", "aggregate", "compute_hhi"]
    assert [step.tool for step in query] == ["query", "compute_hhi"]


def test_grounding_maps_known_aliases_to_catalog_identifiers() -> None:
    """Given LLM aliases, grounding rewrites them to canonical enum values."""

    catalog = CompositionCatalog.from_store(_store())
    raw = {
        "intent_id": "livaro_atozet_channel_diff",
        "spec": {
            "source": "UBIST",
            "view": "strategic",
            "market": "ml_006",
            "dimensions": ["brand_nm", "channel"],
            "group_by": ["month", "brand_nm"],
            "metrics": ["sales_value", "market_share"],
            "derive": ["share_delta"],
            "sort": "value_desc",
            "limit": 5,
        },
    }

    grounded = ground_plan(raw, "query_spec", catalog)

    assert grounded.final_errors == ()
    assert grounded.plan["spec"]["source"] == "ubist"
    assert grounded.plan["spec"]["view"] == "market_landscape"
    assert grounded.plan["spec"]["dimensions"] == ["product", "channel"]
    assert grounded.plan["spec"]["group_by"] == ["period", "product"]
    assert grounded.plan["spec"]["metrics"] == ["sales", "share"]
    assert grounded.plan["spec"]["derive"] == ["delta"]


def test_market_absent_dimension_remains_schema_error() -> None:
    """Given an absent market dimension, grounding must not invent support."""

    catalog = CompositionCatalog.from_store(_store())
    raw = {
        "intent_id": "nhi_mix_trend",
        "spec": {
            "source": "ubist",
            "view": "market_landscape",
            "market": "ml_006",
            "dimensions": ["nhi_type"],
            "group_by": ["nhi_type", "period"],
            "metrics": ["sales"],
            "derive": ["trend"],
        },
    }

    grounded = ground_plan(raw, "query_spec", catalog)

    assert any("dimensions unknown ['nhi_type']" == error for error in grounded.final_errors)
    assert any("group_by unknown ['nhi_type']" == error for error in grounded.final_errors)


def test_primitive_grounding_maps_unknown_compute_tool() -> None:
    """Given a made-up compute tool, grounding maps known patterns to enum tools."""

    catalog = CompositionCatalog.from_store(_store())
    raw = {
        "intent_id": "market_concentration",
        "steps": [
            {"tool": "fetch", "args": {"market": "ml_006"}},
            {"tool": "compute_market_share", "args": {}},
            {"tool": "compute_concentration", "args": {}},
        ],
    }

    grounded = ground_plan(raw, "primitive", catalog)

    assert grounded.final_errors == ()
    assert [step["tool"] for step in grounded.plan["steps"]] == ["fetch", "compute_share", "compute_hhi"]


def _store() -> MartStore:
    periods = ("2025-04", "2026-01", "2026-02", "2026-03", "2026-04")
    first = _record("리바로", "JW", "PTV", "Statin", "Ox", [8, 11, 9, 11, 10], [4, 5, 4, 5, 4], periods)
    second = _record("아토젯", "오가논", "ATV/EZE", "Statin/EZE", "Gx", [10, 20, 18, 24, 20], [5, 8, 7, 9, 8], periods)
    third = _record("테스트", "T", "PTV", "Statin", "Gx", [7, 100, 100, 100, 270], [3, 40, 35, 40, 36], periods)
    return MartStore((first, second, third))


def _record(
    brand: str,
    company: str,
    molecule: str,
    class_label: str,
    ox_gx: str,
    values: list[float],
    channel_values: list[float],
    periods: tuple[str, ...],
) -> BrandRecord:
    metric_history = {}
    for index, period in enumerate(periods):
        total = values[index]
        metric_history[period] = {"raw_value": total, "ms": total / 3.0, "rank": index + 1}
    channel = {"의원": {period: {"raw_value": channel_values[index]} for index, period in enumerate(periods)}}
    dimension = {"ox_gx": {ox_gx: {period: {"raw_value": values[index]} for index, period in enumerate(periods)}}}
    specialty = {"순환기": {period: {"raw_value": values[index]} for index, period in enumerate(periods)}}
    return BrandRecord(
        brand_name=brand,
        company=company,
        manufacturer=company,
        molecule=molecule,
        class_label=class_label,
        ox_gx=ox_gx,
        metric_history=metric_history,
        channel_data=channel,
        specialty_data=specialty,
        dimension_data=dimension,
    )
