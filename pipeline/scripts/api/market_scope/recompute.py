"""Strategy union metric recomputation from deduped facts."""

from __future__ import annotations

from collections import defaultdict
import math
from typing import Any

from pipeline.scripts.api.market_scope.fact_collector import StrategyFact
from pipeline.scripts.api.market_scope.periods import period_span_years, sort_periods, sorted_period_items


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
    market_size = {
        period: sum(history[period] for history in brand_histories.values())
        for period in periods
    }
    brand_ranking = _brand_ranking(brand_histories, market_size)
    company_ranking = _company_ranking(company_histories, market_size)
    hhi = _hhi_series(brand_histories, market_size)
    focus_history = brand_histories.get(focus_brand_key, {period: 0.0 for period in periods})
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
            "ei_ms_matrix": _ei_ms_matrix(brand_ranking, brand_histories, market_size),
            "growth_contribution": _growth_contribution(brand_histories, market_size),
            "hhi_series_5y": hhi,
            "market_size_series": sorted_period_items(market_size),
            "sources_data": {
                "market_size_series": sorted_period_items(market_size),
                "hhi_series_5y": hhi,
                "cagr_5y_pct": _cagr(market_size),
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
            "cagr_5y": _cagr(focus_history),
            "market_cagr_5y": _cagr(market_size),
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


def _brand_ranking(
    histories: dict[str, dict[str, float]],
    market_size: dict[str, float],
) -> dict[str, list[dict[str, Any]]]:
    """Rank brands per period from union totals."""

    result: dict[str, list[dict[str, Any]]] = {}
    for period in sort_periods(market_size):
        ranked = []
        for brand_key, history in histories.items():
            value = history.get(period, 0.0)
            if value <= 0:
                continue
            ranked.append(
                {
                    "brand_key": brand_key,
                    "brand": brand_key,
                    "rank": 0,
                    "raw_value": value,
                    "ms": _share(value, market_size[period]),
                }
            )
        ranked.sort(key=lambda item: (-float(item["raw_value"]), str(item["brand_key"])))
        for index, item in enumerate(ranked, start=1):
            item["rank"] = index
        result[period] = ranked
    return result


def _company_ranking(
    histories: dict[str, dict[str, float]],
    market_size: dict[str, float],
) -> dict[str, list[dict[str, Any]]]:
    """Rank companies per period from union totals."""

    result: dict[str, list[dict[str, Any]]] = {}
    for period in sort_periods(market_size):
        ranked = []
        for company, history in histories.items():
            value = history.get(period, 0.0)
            if value <= 0:
                continue
            ranked.append(
                {
                    "company": company,
                    "rank": 0,
                    "raw_value": value,
                    "ms": _share(value, market_size[period]),
                }
            )
        ranked.sort(key=lambda item: (-float(item["raw_value"]), str(item["company"])))
        for index, item in enumerate(ranked, start=1):
            item["rank"] = index
        result[period] = ranked
    return result


def _hhi_series(
    histories: dict[str, dict[str, float]],
    market_size: dict[str, float],
) -> dict[str, float]:
    """Compute HHI from union brand shares, never from market-level HHI."""

    result: dict[str, float] = {}
    for period, total in sorted_period_items(market_size).items():
        if total <= 0:
            result[period] = 0.0
            continue
        result[period] = round(
            sum(math.pow(_share(history.get(period, 0.0), total), 2) for history in histories.values()),
            6,
        )
    return result


def _ei_ms_matrix(
    ranking: dict[str, list[dict[str, Any]]],
    histories: dict[str, dict[str, float]],
    market_size: dict[str, float],
) -> list[dict[str, Any]]:
    """Build a latest-period EI/MS matrix from recomputed histories."""

    if not market_size:
        return []
    latest = sort_periods(market_size)[-1]
    market_cagr = _cagr(market_size)
    result = []
    for item in ranking.get(latest, []):
        brand_key = str(item["brand_key"])
        brand_cagr = _cagr(histories[brand_key])
        result.append(
            {
                "brand_key": brand_key,
                "brand": item["brand"],
                "period": latest,
                "ms": item["ms"],
                "ei_5y": (brand_cagr / market_cagr * 100.0) if market_cagr not in (None, 0) and brand_cagr is not None else None,
            }
        )
    return result


def _growth_contribution(
    histories: dict[str, dict[str, float]],
    market_size: dict[str, float],
) -> dict[str, list[dict[str, Any]]]:
    """Compute brand growth contribution from union period deltas."""

    periods = sort_periods(market_size)
    if len(periods) < 2:
        return {}
    previous, latest = periods[-2], periods[-1]
    market_delta = market_size[latest] - market_size[previous]
    rows = []
    for brand_key, history in histories.items():
        delta = history[latest] - history[previous]
        rows.append(
            {
                "brand_key": brand_key,
                "brand": brand_key,
                "growth_contribution": (delta / market_delta * 100.0) if market_delta else None,
            }
        )
    rows.sort(key=lambda item: abs(float(item["growth_contribution"] or 0.0)), reverse=True)
    return {latest: rows}


def _cagr(history: dict[str, float]) -> float | None:
    """Compute annualized growth from first to last positive history point."""

    positive = [(period, history[period]) for period in sort_periods(history) if history[period] > 0]
    if len(positive) < 2:
        return None
    first_period, first_value = positive[0]
    last_period, last_value = positive[-1]
    years = period_span_years(first_period, last_period)
    if years <= 0:
        return None
    return round((math.pow(last_value / first_value, 1 / years) - 1) * 100, 6)


def _share(value: float, total: float) -> float:
    """Return percentage share rounded for JSON stability."""

    return round((value / total * 100.0) if total else 0.0, 6)


def _brand_name(facts: tuple[StrategyFact, ...], brand_key: str) -> str:
    """Return display brand for a focus key."""

    for fact in facts:
        if fact.brand_key == brand_key:
            return fact.brand_name
    return brand_key
