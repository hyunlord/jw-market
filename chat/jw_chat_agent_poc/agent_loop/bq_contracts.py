from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Literal, TypeAlias


BqContractId: TypeAlias = Literal[
    "A1", "A2", "A3", "B1", "B2", "B3", "C1", "C2", "C3",
    "D1", "D2", "D3", "E1", "E2",
]


class SlotTier(str, Enum):
    REQUIRED = "required"
    BUSINESS_REQUIRED = "business_required"
    OPTIONAL = "optional"


class SlotStatus(str, Enum):
    SUPPORTED = "supported"
    REQUIRED_BUT_UNAVAILABLE = "required_but_unavailable"
    MISSING = "missing"
    NOT_APPLICABLE = "not_applicable"


@dataclass(frozen=True, slots=True)
class AnalysisSlot:
    slot_id: str
    tier: SlotTier
    evidence_keys: tuple[str, ...]
    supporting_tools: tuple[str, ...]
    required_sources: tuple[str, ...] = ()
    require_all_evidence: bool = False


@dataclass(frozen=True, slots=True)
class SlotCoverage:
    slot_id: str
    tier: SlotTier
    status: SlotStatus
    supporting_tools: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BqContract:
    contract_id: BqContractId
    required_slots: tuple[str, ...]
    tools: tuple[str, ...]
    sources: tuple[str, ...]
    calculations: tuple[str, ...]
    safety_rules: tuple[str, ...]
    chart_kinds: tuple[str, ...]

    @property
    def analysis_slots(self) -> tuple[AnalysisSlot, ...]:
        return _ANALYSIS_SLOTS[self.contract_id]

    @property
    def forbidden_outputs(self) -> tuple[str, ...]:
        return _FORBIDDEN_OUTPUTS.get(self.contract_id, ())


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


def _slot(
    slot_id: str,
    tier: SlotTier,
    evidence_keys: tuple[str, ...],
    supporting_tools: tuple[str, ...],
    *,
    required_sources: tuple[str, ...] = (),
    require_all: bool = False,
) -> AnalysisSlot:
    return AnalysisSlot(
        slot_id,
        tier,
        evidence_keys,
        supporting_tools,
        required_sources,
        require_all,
    )


R = SlotTier.REQUIRED
BR = SlotTier.BUSINESS_REQUIRED
O = SlotTier.OPTIONAL

# These declarations are the executable transcription of the fourteen BQ rows in
# JW_Chat_BQ_질문세트_v1.xlsx. They describe required outputs, not routing words.
_ANALYSIS_SLOTS: Mapping[BqContractId, tuple[AnalysisSlot, ...]] = MappingProxyType({
    "A1": (
        _slot("latest_market_sales", R, ("end_sales_krw",), ("get_brand_series",)),
        _slot("market_trend_3_5y", R, ("source_summaries",), ("get_brand_series",)),
        _slot("market_cagr", R, ("growth_rate_pct",), ("get_brand_series",)),
        _slot("channel_breakdown", BR, ("channel_shares_pct",), ("get_brand_channel_breakdown",)),
        _slot("market_direction_verdict", BR, ("insights",), ("get_brand_series",)),
        _slot("specialty_breakdown", O, ("specialty_shares_pct",), ("get_brand_specialty_breakdown",)),
    ),
    "A2": (
        _slot("trend_extension_forecast", R, ("forecast_krw",), ("get_brand_series",)),
        _slot("forecast_basis", R, ("trend_rate_pct",), ("get_brand_series",)),
        _slot("forecast_uncertainty", R, ("forecast_uncertainty",), ("get_brand_series",)),
        _slot("patient_trend", BR, ("patient_count",), ("get_disease_stats",), required_sources=("hira",)),
        _slot("impacting_issues", BR, ("news_items",), ("search_news",), required_sources=("news",)),
        _slot("scenario_range", O, ("scenario_range",), ("get_brand_series",)),
    ),
    "A3": (
        _slot("patient_count", R, ("patient_count",), ("get_disease_stats",), required_sources=("hira",)),
        _slot("market_sales", R, ("sales_krw",), ("get_brand_sales",)),
        _slot("source_separation_limit", R, ("never_aggregate_sources",), ("get_disease_stats", "get_brand_sales")),
        _slot("period_definition_alignment", BR, ("patient_period", "period"), ("get_disease_stats", "get_brand_sales"), require_all=True),
        _slot("penetration_interpretation", BR, ("insights",), ("get_disease_stats", "get_brand_sales")),
        _slot("sales_per_patient", O, ("sales_per_patient_krw",), ("get_disease_stats", "get_brand_sales")),
        _slot("patient_subgroup", O, ("patient_subgroup",), ("get_disease_stats",)),
    ),
    "B1": (
        _slot("comparison_period", R, ("period",), ("get_brand_series",)),
        _slot("current_top_structure", R, ("current_top_structure",), ("get_top_brands",)),
        _slot("share_gainers", R, ("share_gainers",), ("get_top_brands",)),
        _slot("share_losers", R, ("share_losers",), ("get_top_brands",)),
        _slot("competition_change_conclusion", R, ("competition_change_conclusion",), ("get_top_brands",)),
        _slot("own_share_rank_change", BR, ("share_delta_pctp",), ("get_brand_series", "get_top_brands")),
        _slot("rank_changes", BR, ("gain_loss",), ("get_top_brands",)),
        _slot("share_of_growth", O, ("share_of_growth_pct",), ("get_brand_series",)),
        _slot("growth_decomposition", O, ("market_growth_pct", "excess_growth_pctp"), ("get_brand_series",), require_all=True),
        _slot("concentration_change", O, ("hhi_change", "top3_concentration_change"), ("get_top_brands",)),
        _slot("related_events", O, ("news_items",), ("search_news",)),
        _slot("channel_competition_change", O, ("channel_competition_change",), ("get_brand_channel_breakdown",)),
    ),
    "B2": (
        _slot("competitor_definition", R, ("competition_basis",), ("get_market_scope",)),
        _slot("own_position", R, ("cohort_z_score",), ("get_brand_series", "get_top_brands")),
        _slot("competitor_sales_share_growth", R, ("segments", "source_results"), ("get_top_brands",)),
        _slot("cohort_z_score", BR, ("cohort_z_score",), ("get_top_brands",)),
        _slot("cohort_population", BR, ("population",), ("get_top_brands",)),
        _slot("channel_position", O, ("channel_position",), ("get_brand_channel_breakdown",)),
    ),
    "B3": (
        _slot("high_growth_share_gain", R, ("growth_ranking",), ("get_top_brands",)),
        _slot("launch_relative_acceleration", R, ("launch_acceleration",), ("get_top_brands", "search_news")),
        _slot("threat_evidence", R, ("share_delta_pctp",), ("get_top_brands",)),
        _slot("connected_issues", BR, ("news_items",), ("search_news",), required_sources=("news",)),
        _slot("threat_verdict", BR, ("insights",), ("get_top_brands",)),
        _slot("pipeline_entries", O, ("pipeline_entries",), ("search_news",)),
    ),
    "C1": (
        _slot("brand_sales_series", R, ("brand_growth_pct",), ("get_brand_series",)),
        _slot("prescription_series", R, ("prescription_series",), ("get_brand_series",)),
        _slot("change_rate", R, ("brand_growth_pct",), ("get_brand_series",)),
        _slot("market_comparison", BR, ("market_growth_pct", "growth_gap_pctp"), ("get_brand_series",), require_all=True),
        _slot("co_movement_verdict", BR, ("insights",), ("get_brand_series",)),
        _slot("channel_trend", O, ("channel_trend",), ("get_brand_channel_breakdown",)),
    ),
    "C2": (
        _slot("channel_distribution", R, ("channel_shares_pct",), ("get_brand_channel_breakdown",)),
        _slot("specialty_distribution", R, ("specialty_shares_pct",), ("get_brand_specialty_breakdown",)),
        _slot("channel_growth_difference", R, ("axis_growth",), ("get_brand_series",)),
        _slot("leading_axis_verdict", BR, ("insights",), ("get_brand_channel_breakdown", "get_brand_specialty_breakdown")),
        _slot("market_axis_comparison", BR, ("market_axis_comparison",), ("get_brand_series",)),
        _slot("customer_detail", O, ("customer_detail",), ("get_brand_channel_breakdown",)),
    ),
    "C3": (
        _slot("ubist_value", R, ("ubist_sales_krw",), ("get_brand_series",), required_sources=("ubist",)),
        _slot("iqvia_value", R, ("iqvia_sales_krw",), ("get_brand_series",), required_sources=("iqvia_nsa",)),
        _slot("source_divergence", R, ("absolute_delta_krw", "relative_delta_pct"), ("get_brand_series",), require_all=True),
        _slot("lag_interpretation", BR, ("source_lag",), ("get_brand_series",)),
        _slot("inventory_signal", BR, ("inventory_signal",), ("get_brand_series",)),
        _slot("channel_reconciliation", O, ("channel_reconciliation",), ("get_brand_channel_breakdown",)),
    ),
    "D1": (
        _slot("activity_trend", R, ("activity_trend", "series"), ("csd_activity_trend",)),
        _slot("topic_distribution", R, ("topic_share",), ("csd_activity_trend",)),
        _slot("period_change", R, ("activity_change_rate_pct",), ("csd_activity_trend",)),
        _slot("total_region_basis", BR, ("region",), ("csd_activity_trend",)),
        _slot("activity_verdict", BR, ("insights",), ("csd_activity_trend",)),
        _slot("seller_breakdown", O, ("seller_series",), ("csd_activity_trend",)),
    ),
    "D2": (
        _slot("activity_change", R, ("activity_change_rate_pct",), ("csd_activity_trend",)),
        _slot("performance_change", R, ("performance_change_rate_pct",), ("get_brand_series",)),
        _slot("temporal_alignment", R, ("period",), ("csd_activity_trend", "get_brand_series")),
        _slot("noncausal_verdict", BR, ("temporal_overlap_not_causation",), ("csd_activity_trend", "get_brand_series")),
        _slot("source_separation", BR, ("never_aggregate_sources",), ("get_brand_series",)),
        _slot("topic_alignment", O, ("topic_alignment",), ("csd_activity_trend",)),
    ),
    "D3": (
        _slot("seller_activity_share_change", R, ("seller_activity_share_delta",), ("csd_activity_trend",)),
        _slot("comparison_periods", R, ("period",), ("csd_activity_trend",)),
        _slot("competitor_sellers", R, ("seller_series",), ("csd_activity_trend",)),
        _slot("total_region_basis", BR, ("region",), ("csd_activity_trend",)),
        _slot("seller_change_verdict", BR, ("insights",), ("csd_activity_trend",)),
        _slot("topic_by_seller", O, ("topic_by_seller",), ("csd_activity_trend",)),
    ),
    "E1": (
        _slot("news_identity", R, ("news_items", "web_sources"), ("search_news", "web_search")),
        _slot("title_date_source_url", R, ("title", "date", "source", "url"), ("search_news", "web_search"), require_all=True),
        _slot("issue_summary", R, ("summary", "snippet"), ("search_news", "web_search")),
        _slot("brand_relevance", BR, ("brand_relevance", "relevance"), ("search_news", "web_search")),
        _slot("single_source_block", BR, ("source_block",), ("search_news", "web_search")),
        _slot("issue_timeline", O, ("timeline",), ("search_news",)),
    ),
    "E2": (
        _slot("quantitative_change", R, ("share_of_growth_pct", "share_delta_pctp"), ("get_brand_series", "get_top_brands")),
        _slot("issue_timing", R, ("temporal_alignment",), ("search_news", "get_brand_series")),
        _slot("external_evidence", R, ("news_items", "web_sources"), ("search_news",)),
        _slot("grounded_causal_interpretation", R, ("insights",), ("get_brand_series", "search_news")),
        _slot("source_divergence", BR, ("source_divergence",), ("get_brand_series",)),
        _slot("activity_patient_context", BR, ("activity_alignment", "patient_count"), ("csd_activity_trend", "get_disease_stats")),
        _slot("additional_events", O, ("additional_events",), ("search_news",)),
    ),
})

_FORBIDDEN_OUTPUTS: Mapping[BqContractId, tuple[str, ...]] = MappingProxyType({
    "B1": ("single_period_snapshot_only", "irrelevant_source_failure"),
})


def contract_for(contract_id: str) -> BqContract | None:
    return _BY_ID.get(contract_id)


def evaluate_slot_coverage(
    contract_id: str,
    analysis_call: Mapping[str, Any] | None,
    *,
    missing_sources: tuple[str, ...] = (),
) -> tuple[SlotCoverage, ...]:
    contract = contract_for(contract_id)
    if contract is None:
        return ()
    data = analysis_call.get("render_data", {}) if analysis_call is not None else {}
    unavailable = {source.casefold() for source in missing_sources}
    coverage: list[SlotCoverage] = []
    for slot in contract.analysis_slots:
        matches = tuple(_contains_evidence(data, key) for key in slot.evidence_keys)
        supported = all(matches) if slot.require_all_evidence else any(matches)
        if supported:
            status = SlotStatus.SUPPORTED
        elif slot.tier is SlotTier.OPTIONAL:
            status = SlotStatus.NOT_APPLICABLE
        elif unavailable.intersection(source.casefold() for source in slot.required_sources):
            status = SlotStatus.REQUIRED_BUT_UNAVAILABLE
        else:
            status = SlotStatus.MISSING
        coverage.append(SlotCoverage(slot.slot_id, slot.tier, status, slot.supporting_tools))
    return tuple(coverage)


def missing_slot_tools(coverage: tuple[SlotCoverage, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(
        tool
        for item in coverage
        if item.status is SlotStatus.MISSING and item.tier is not SlotTier.OPTIONAL
        for tool in item.supporting_tools
    ))


def _contains_evidence(value: Any, key: str) -> bool:
    if isinstance(value, Mapping):
        if key in value and _is_present(value[key]):
            return True
        return any(_contains_evidence(child, key) for child in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_evidence(child, key) for child in value)
    return False


def _is_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict)):
        return bool(value)
    return True
