from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, TypeAlias


BqContractId: TypeAlias = Literal[
    "A1", "A2", "A3", "B1", "B2", "B3", "C1", "C2", "C3",
    "D1", "D2", "D3", "E1", "E2",
]


@dataclass(frozen=True, slots=True)
class BqContract:
    contract_id: BqContractId
    required_slots: tuple[str, ...]
    tools: tuple[str, ...]
    sources: tuple[str, ...]
    calculations: tuple[str, ...]
    safety_rules: tuple[str, ...]
    chart_kinds: tuple[str, ...]


_BASE_SAFETY = (
    "preserve_missing_values",
    "evidence_required_for_every_claim",
    "preserve_requested_scope",
)
_MARKET_SAFETY = (*_BASE_SAFETY, "market_landscape_definition")


BQ_CONTRACTS: tuple[BqContract, ...] = (
    BqContract(
        "A1", ("brand", "period"),
        ("get_brand_series", "get_brand_channel_breakdown"),
        ("UBIST", "IQVIA_NSA"), ("cagr", "channel_share", "trend_direction"),
        (*_MARKET_SAFETY, "never_aggregate_sources"), ("line", "bar"),
    ),
    BqContract(
        "A2", ("brand", "period"),
        ("get_brand_series", "get_disease_stats", "search_news"),
        ("UBIST", "IQVIA_NSA", "HIRA", "NEWS"),
        ("conditional_trend_forecast", "patient_cagr", "temporal_alignment"),
        (*_MARKET_SAFETY, "forecast_is_trend_extension", "forecast_uncertainty", "never_aggregate_sources"),
        ("line",),
    ),
    BqContract(
        "A3", ("brand", "period"),
        ("get_disease_stats", "get_brand_sales", "get_brand_share"),
        ("HIRA", "UBIST", "IQVIA_NSA"), ("patient_sales_ratio",),
        (*_MARKET_SAFETY, "single_brand_only", "never_aggregate_sources"), ("line",),
    ),
    BqContract(
        "B1", ("brand", "period"),
        ("get_brand_series", "get_top_brands"), ("UBIST", "IQVIA_NSA"),
        ("share_delta", "share_of_growth", "growth_decomposition", "gain_loss"),
        (*_MARKET_SAFETY, "change_narrative_required", "never_aggregate_sources"),
        ("line", "waterfall"),
    ),
    BqContract(
        "B2", ("brand", "period"),
        ("get_market_scope", "get_brand_series", "get_top_brands"),
        ("UBIST", "IQVIA_NSA"), ("cohort_z_score", "relative_growth"),
        (*_MARKET_SAFETY, "competition_definition_required", "never_aggregate_sources"),
        ("scatter", "bar"),
    ),
    BqContract(
        "B3", ("brand", "period"),
        ("get_top_brands", "get_brand_series", "search_news"),
        ("UBIST", "IQVIA_NSA", "NEWS"), ("growth_rank", "launch_acceleration"),
        (*_MARKET_SAFETY, "threat_requires_share_erosion", "relevant_news_only", "never_aggregate_sources"),
        ("scatter",),
    ),
    BqContract(
        "C1", ("brand", "period"),
        ("get_brand_series", "get_brand_sales"), ("UBIST", "IQVIA_NSA"),
        ("brand_market_growth_gap", "trend_slope"),
        (*_MARKET_SAFETY, "single_brand_only", "never_aggregate_sources"), ("line",),
    ),
    BqContract(
        "C2", ("brand", "period", "axis"),
        ("get_brand_channel_breakdown", "get_brand_specialty_breakdown", "get_brand_series"),
        ("UBIST",), ("channel_share", "specialty_share", "axis_growth"),
        (*_MARKET_SAFETY, "distribution_bottleneck_requires_numbers"), ("bar",),
    ),
    BqContract(
        "C3", ("brand", "period", "source"),
        ("get_brand_series",), ("UBIST", "IQVIA_NSA"),
        ("source_divergence", "source_lag"),
        (*_MARKET_SAFETY, "never_aggregate_sources", "source_tags_required"), ("line",),
    ),
    BqContract(
        "D1", ("brand", "period"),
        ("csd_activity_trend",), ("CSD",), ("activity_trend", "topic_share"),
        (*_BASE_SAFETY, "csd_total_region_only", "exclude_market2"), ("line", "bar"),
    ),
    BqContract(
        "D2", ("brand", "period"),
        ("csd_activity_trend", "get_brand_series"), ("CSD", "UBIST", "IQVIA_NSA"),
        ("activity_performance_alignment",),
        (*_MARKET_SAFETY, "temporal_overlap_not_causation", "csd_total_region_only", "never_aggregate_sources"),
        ("dual_axis",),
    ),
    BqContract(
        "D3", ("brand", "period"),
        ("get_market_scope", "csd_activity_trend"), ("CSD",),
        ("seller_activity_share_delta",),
        (*_BASE_SAFETY, "csd_total_region_only", "exclude_market2"), ("bar",),
    ),
    BqContract(
        "E1", ("brand", "period"),
        ("search_news", "web_search"), ("NEWS", "WEB"),
        ("brand_relevance",),
        (*_BASE_SAFETY, "relevant_news_only", "cited_news_must_be_used", "news_identity_required"),
        ("timeline",),
    ),
    BqContract(
        "E2", ("brand", "period"),
        ("get_brand_series", "get_top_brands", "search_news", "csd_activity_trend", "get_disease_stats"),
        ("UBIST", "IQVIA_NSA", "HIRA", "CSD", "NEWS"),
        ("share_of_growth", "gain_loss", "source_divergence", "temporal_alignment"),
        (*_MARKET_SAFETY, "never_aggregate_sources", "temporal_overlap_not_causation", "relevant_news_only"),
        ("line", "waterfall", "dual_axis"),
    ),
)

BQ_CONTRACT_IDS: tuple[BqContractId, ...] = tuple(item.contract_id for item in BQ_CONTRACTS)
_BY_ID: Mapping[str, BqContract] = MappingProxyType(
    {item.contract_id: item for item in BQ_CONTRACTS}
)


def contract_for(contract_id: str) -> BqContract | None:
    return _BY_ID.get(contract_id)
