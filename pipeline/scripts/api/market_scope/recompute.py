"""Strategy union metric recomputation from deduped facts."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from pipeline.scripts.api.market_scope.archive_metrics import (
    annual_hhi_series,
    annual_ranking_payload,
    ei_ms_matrix_payload,
    endpoint_ei_with_fallback,
)
from pipeline.scripts.api.market_scope.archive_growth import growth_contribution_payload
from pipeline.scripts.api.market_scope.fact_collector import StrategyFact
from pipeline.scripts.api.market_scope.periods import sort_periods, sorted_period_items


def recompute_strategy_payload(
    facts: tuple[StrategyFact, ...],
    *,
    focus_brand_key: str,
    source: str,
    measure: str,
) -> dict[str, Any]:
    """Recompute cause-compatible strategy metrics for a union scope."""

    periods = _periods(facts)
    brand_histories = _brand_histories(facts, periods)
    company_histories = _company_histories(facts, periods)
    brand_names = _brand_names(facts)
    companies = _brand_companies(facts)
    market_size = {
        period: sum(history[period] for history in brand_histories.values())
        for period in periods
    }
    brand_ranking = annual_ranking_payload(
        brand_histories,
        label_key="brand_key",
        focus_id=focus_brand_key,
        display_names=brand_names,
    )
    focus_company = companies.get(focus_brand_key)
    company_ranking = annual_ranking_payload(
        company_histories,
        label_key="company",
        focus_id=focus_company,
    )
    hhi = annual_hhi_series(brand_histories, source=source)
    hhi_recent = hhi[-1]["hhi"] if hhi else None
    focus_history = brand_histories.get(focus_brand_key, {period: 0.0 for period in periods})
    focus_ei = endpoint_ei_with_fallback(focus_history, market_size)
    source_label = "UBIST" if source.lower() == "ubist" else source.upper()
    return {
        "brand": _brand_name(facts, focus_brand_key),
        "brand_key": focus_brand_key,
        "data": {
            "analysis_level_market_status": {},
            "analysis_levels": {},
            "brand_ranking": brand_ranking,
            "brand_ranking_stacked": brand_ranking,
            "company_ranking": company_ranking,
            "company_ranking_stacked": company_ranking,
            "ei_ms_matrix": ei_ms_matrix_payload(
                brand_histories,
                market_size,
                focus_brand_key=focus_brand_key,
                brand_names=brand_names,
                companies=companies,
            ),
            "growth_contribution": growth_contribution_payload(
                brand_histories,
                source=source,
                focus_brand_key=focus_brand_key,
                brand_names=brand_names,
                companies=companies,
            ),
            "hhi_series_5y": hhi,
            "hhi_recent": hhi_recent,
            "market_size_series": sorted_period_items(market_size),
            "sources_data": {
                "market_size_series": sorted_period_items(market_size),
                "hhi_series_5y": hhi,
                "hhi_recent": hhi_recent,
                "cagr_5y_pct": focus_ei.get("market_cagr_pct"),
            },
            "target_customer_competition": {
                "available_in_view": [],
                "target_type": "strategy_union",
                "targets": [],
                "views": [],
            },
            "target_customer_competition_by_channel": {},
            "ubist_specialty_channels": [],
            "ubist_specialty_target_channels": [],
        },
        "market_id": "scope:unresolved",
        "measure": measure,
        "source": source_label,
        "summary": {
            "market_share": _share(focus_history.get(periods[-1], 0.0), market_size.get(periods[-1], 0.0)) if periods else 0.0,
            "cagr_5y": focus_ei.get("brand_cagr_pct"),
            "market_cagr_5y": focus_ei.get("market_cagr_pct"),
        },
        "unit_label": facts[0].unit_label if facts else "",
        "view": "market_landscape",
    }


def _periods(facts: tuple[StrategyFact, ...]) -> tuple[str, ...]:
    """Return all periods present in the candidate facts."""

    return sort_periods({period for fact in facts for period in fact.raw_value_history})


def _brand_histories(
    facts: tuple[StrategyFact, ...],
    periods: tuple[str, ...],
) -> dict[str, dict[str, float]]:
    """Aggregate fact histories at brand grain."""

    histories: dict[str, dict[str, float]] = defaultdict(lambda: {period: 0.0 for period in periods})
    for fact in facts:
        for period in periods:
            histories[fact.brand_key][period] += float(fact.raw_value_history.get(period, 0.0))
    return dict(histories)


def _company_histories(
    facts: tuple[StrategyFact, ...],
    periods: tuple[str, ...],
) -> dict[str, dict[str, float]]:
    """Aggregate fact histories at company grain."""

    histories: dict[str, dict[str, float]] = defaultdict(lambda: {period: 0.0 for period in periods})
    for fact in facts:
        for period in periods:
            histories[fact.company][period] += float(fact.raw_value_history.get(period, 0.0))
    return dict(histories)


def _share(value: float, total: float) -> float:
    """Return percentage share rounded for JSON stability."""

    return round((value / total * 100.0) if total else 0.0, 6)


def _brand_name(facts: tuple[StrategyFact, ...], brand_key: str) -> str:
    """Return display brand for a focus key."""

    for fact in facts:
        if fact.brand_key == brand_key:
            return fact.brand_name
    return brand_key


def _brand_names(facts: tuple[StrategyFact, ...]) -> dict[str, str]:
    """Return display names keyed by brand key."""

    return {fact.brand_key: fact.brand_name for fact in facts}


def _brand_companies(facts: tuple[StrategyFact, ...]) -> dict[str, str]:
    """Return company names keyed by brand key."""

    return {fact.brand_key: fact.company for fact in facts}
