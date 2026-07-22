"""Strategy union metric recomputation from deduped facts."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from pipeline.scripts.api.composers.cache_to_response import compose_cached_json
from pipeline.scripts.api.dynamic_market.cause_payload import normalize_portal_read_data
from pipeline.scripts.api.market_scope.archive_metrics import (
    annual_hhi_series,
    annual_ranking_payload,
    ei_ms_matrix_payload,
    endpoint_ei_with_fallback,
)
from pipeline.scripts.api.market_scope.archive_growth import growth_contribution_payload
from pipeline.scripts.api.market_scope.fact_collector import StrategyFact
from pipeline.scripts.api.market_scope.legacy_shape import (
    LegacyMarketMetaInput,
    empty_analysis_levels,
    empty_company_concentration_trend,
    empty_level_top5_trend,
    empty_matrix,
    empty_target_customer_competition,
    market_meta,
    market_size_series_payload,
    market_yoy_series,
    period_unit,
)
from pipeline.scripts.api.market_scope.periods import sort_periods, sorted_period_items
from pipeline.scripts.etl.cache_build_common import market_cagr_exclusive


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
    ei_matrix = ei_ms_matrix_payload(
        brand_histories,
        market_size,
        focus_brand_key=focus_brand_key,
        brand_names=brand_names,
        companies=companies,
    )
    source_label = "UBIST" if source.lower() == "ubist" else source.upper()
    focus_brand_name = _brand_name(facts, focus_brand_key)
    market_size_points = market_size_series_payload(sorted_period_items(market_size), source=source)
    yoy_series = market_yoy_series(sorted_period_items(market_size))
    kpi = _kpi(
        brand_histories=brand_histories,
        market_size=market_size,
        focus_brand_key=focus_brand_key,
        focus_ei=focus_ei,
        hhi_recent=hhi_recent,
        ei_matrix=ei_matrix,
        periods=periods,
    )
    scope_market_id = "scope:unresolved"
    raw_payload = {
        "brand": focus_brand_name,
        "brand_key": focus_brand_key,
        "brand_name": focus_brand_name,
        "data": normalize_portal_read_data({
            "analysis_level_market_status": empty_analysis_levels(periods, source=source),
            "analysis_levels": empty_analysis_levels(periods, source=source),
            "brand_ranking": brand_ranking,
            "brand_ranking_stacked": brand_ranking,
            "company_concentration_trend": empty_company_concentration_trend(),
            "company_ranking": company_ranking,
            "company_ranking_stacked": company_ranking,
            "ei_ms_matrix": ei_matrix,
            "growth_contribution": growth_contribution_payload(
                brand_histories,
                source=source,
                focus_brand_key=focus_brand_key,
                brand_names=brand_names,
                companies=companies,
            ),
            "growth_contribution_ms_matrix": empty_matrix(),
            "hhi_series_5y": hhi,
            "hhi_recent": hhi_recent,
            "kpi": kpi,
            "level_top5_trend": empty_level_top5_trend(),
            "market_size_series": market_size_points,
            "market_yoy_recent_pct": yoy_series.get(periods[-1]) if periods else None,
            "market_yoy_series": yoy_series,
            "sources_data": {
                "periods_unit": period_unit(source),
                "periods_count": len(periods),
                "market_size_series": market_size_points,
                "market_yoy_series": yoy_series,
                "market_yoy_recent_pct": yoy_series.get(periods[-1]) if periods else None,
                "hhi_series_5y": hhi,
                "hhi_recent": hhi_recent,
                "cagr_5y_pct": focus_ei.get("market_cagr_pct"),
            },
            "target_customer_competition": empty_target_customer_competition(),
            "target_customer_competition_by_channel": {},
            "ubist_specialty_channels": [],
            "ubist_specialty_target_channels": [],
        }),
        "market_id": scope_market_id,
        "market_meta": market_meta(
            LegacyMarketMetaInput(
                market_id=scope_market_id,
                source_label=source_label,
                measure=measure,
                direct_competition_count=kpi["direct_competition_count"],
                market_size_recent=kpi["market_size_recent"],
                market_cagr_5y_pct=kpi["market_cagr_5y_pct"],
            )
        ),
        "measure": measure,
        "source": source_label,
        "unit_label": facts[0].unit_label if facts else "",
        "view": "market_landscape",
    }
    # Reuse the same FE-facing adapter as cache fast-path responses. This is
    # what turns market_size_series dicts into point arrays and preserves future
    # composer aliases in one place instead of forking a scoped-only contract.
    payload = compose_cached_json(raw_payload, measure=measure)
    if not isinstance(payload, dict):
        raise TypeError("strategy recompute payload must normalize to a JSON object")
    return payload


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


def _kpi(
    *,
    brand_histories: dict[str, dict[str, float]],
    market_size: dict[str, float],
    focus_brand_key: str,
    focus_ei: dict[str, Any],
    hhi_recent: float | None,
    ei_matrix: dict[str, Any],
    periods: tuple[str, ...],
) -> dict[str, Any]:
    """Build the FE KPI aliases expected by the cause dashboard."""

    latest = periods[-1] if periods else None
    market_recent = market_size.get(latest, 0.0) if latest else 0.0
    focus_recent = brand_histories.get(focus_brand_key, {}).get(latest, 0.0) if latest else 0.0
    target_row = next(
        (
            row
            for row in ei_matrix.get("data", [])
            if isinstance(row, dict) and row.get("brand_key") == focus_brand_key
        ),
        {},
    )
    positive_latest_brands = [
        history
        for history in brand_histories.values()
        if latest is not None and history.get(latest, 0.0) > 0
    ]
    market_cagr_5y, market_cagr_3y = market_cagr_exclusive(market_size)
    return {
        "market_size_recent": market_recent,
        "market_cagr_5y_pct": market_cagr_5y,
        "market_cagr_3y_pct": market_cagr_3y,
        "top3_share_pct": _top_share(ei_matrix, limit=3),
        "hhi_recent": hhi_recent,
        "direct_competition_count": len(positive_latest_brands),
        "target_brand": target_row.get("brand"),
        "target_company": target_row.get("company"),
        "target_ei": target_row.get("ei"),
        "ei": target_row.get("ei"),
        "ei_basis": target_row.get("ei_basis"),
        "ei_period_years": target_row.get("ei_period_years"),
        "ei_note": target_row.get("ei_note"),
        "brand_cagr_pct": focus_ei.get("brand_cagr_pct"),
        "market_cagr_pct": focus_ei.get("market_cagr_pct"),
        "target_momentum": target_row.get("momentum_score"),
        "target_rank": target_row.get("rank"),
        "target_share_pct": target_row.get("share_pct", _share(focus_recent, market_recent)),
        "brand_value_recent": focus_recent,
        "brand_share_pct": target_row.get("share_pct", _share(focus_recent, market_recent)),
        "momentum_score": target_row.get("momentum_score"),
    }


def _top_share(ei_matrix: dict[str, Any], *, limit: int) -> float:
    """Return recent top-N share from the already ranked matrix rows."""

    rows = [row for row in ei_matrix.get("data", []) if isinstance(row, dict)]
    shares = [float(row.get("share_pct") or 0.0) for row in rows[:limit]]
    return round(sum(shares), 4)


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
