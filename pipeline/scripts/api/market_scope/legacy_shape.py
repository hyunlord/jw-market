"""Legacy ``cache_cause`` shape helpers for scoped strategy recompute."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pipeline.scripts.api.market_growth import compound_period_growth_pct


@dataclass(frozen=True, slots=True)
class LegacyMarketMetaInput:
    """Inputs needed to emit the legacy ``market_meta`` keyset."""

    market_id: str
    source_label: str
    measure: str
    direct_competition_count: int
    market_size_recent: float
    market_cagr_5y_pct: float | None


def market_size_series_payload(
    market_size: Mapping[str, float],
    *,
    source: str,
) -> dict[str, dict[str, float | None]]:
    """Return composer-ready market-size points with annual growth fields."""

    yoy_series = market_yoy_series(market_size)
    mom_series = market_cmgr_series(market_size, source=source)
    return {
        period: {"value": value, "yoy_growth_pct": yoy_series[period], "mom_growth_pct": mom_series[period]}
        for period, value in market_size.items()
    }


def market_yoy_series(market_size: Mapping[str, float]) -> dict[str, float | None]:
    """Return legacy period-to-prior-year growth percentages."""

    return {
        period: _pct_change(market_size.get(_previous_year_period(period)), value)
        if _previous_year_period(period) in market_size
        else None
        for period, value in market_size.items()
    }


def market_cmgr_series(market_size: Mapping[str, float], *, source: str) -> dict[str, float | None]:
    """Return compound period growth against each exact prior-year period."""

    periods_per_year = _expected_periods_per_year(source)
    return {
        period: compound_period_growth_pct(
            market_size.get(_previous_year_period(period)),
            value,
            periods_per_year,
        )
        if _previous_year_period(period) in market_size
        else None
        for period, value in market_size.items()
    }


def period_coverage(periods: tuple[str, ...], *, source: str) -> dict[str, Any]:
    """Return the legacy ``data_period_coverage`` container."""

    latest = periods[-1] if periods else None
    latest_year = int(latest[:4]) if latest else None
    counts = {
        year: sum(1 for period in periods if period.startswith(year))
        for year in sorted({period[:4] for period in periods})
    }
    expected = _expected_periods_per_year(source)
    latest_count = counts.get(str(latest_year), 0) if latest_year is not None else 0
    return {
        "latest_period": latest,
        "latest_year": latest_year,
        "latest_year_period_count": latest_count,
        "latest_year_is_partial": latest_count < expected,
        "period_count_by_year": counts,
        "expected_periods_per_year": expected,
    }


def empty_analysis_levels(periods: tuple[str, ...], *, source: str) -> dict[str, Any]:
    """Return legacy AnalysisLevels keys without scoped-only extras."""

    return {
        "period_unit": period_unit(source),
        "channels": [],
        "levels": [],
        "periods_monthly": list(periods) if _source_key(source) == "UBIST" else [],
        "periods_quarterly": list(periods) if _source_key(source) != "UBIST" else [],
        "data": {},
    }


def empty_analysis_level_market_status() -> dict[str, Any]:
    """Return legacy ALMS keys for scopes without level overlays."""

    return {
        "available_levels": [],
        "default_level": None,
        "by_level": {},
        "channels": [],
        "by_channel": {},
        "ms_by_channel": {},
        "targets": [],
        "note": "",
    }


def empty_target_customer_competition() -> dict[str, Any]:
    """Return legacy target-competition keys for scopes without targets."""

    return {
        "available_in_view": [],
        "target_type": "strategy_union",
        "targets": [],
        "views": [],
        "note": "",
    }


def empty_level_top5_trend() -> dict[str, Any]:
    """Return legacy level trend keys for scopes without level overlays."""

    return {"available_levels": [], "default_level": None, "by_level": {}, "note": ""}


def empty_matrix() -> dict[str, Any]:
    """Return legacy matrix keys for non-recomputed scoped sections."""

    return {"data": [], "ms_avg_pct": 0.0, "share_avg_pct": 0.0}


def empty_company_concentration_trend() -> dict[str, Any]:
    """Return legacy company concentration trend keys."""

    return {"periods": [], "hhi_values": []}


def market_meta(payload: LegacyMarketMetaInput) -> dict[str, Any]:
    """Return the legacy ``market_meta`` keyset for scoped responses."""

    return {
        "strategic_market_id": payload.market_id,
        "market_name": "Scoped strategy union",
        "market_name_short": "Scoped strategy union",
        "market_label_kor": "Scoped strategy union",
        "market_definition_label": "Scoped strategy union",
        "market_definition_full": "Scoped strategy union",
        "mkt_team": "Runtime",
        "brand_list": [],
        "atc_codes": [],
        "atc_desc": "",
        "view_source_id": "market_scope_union",
        "atc_count": None,
        "nhi_type": None,
        "sources": [payload.source_label],
        "source_label": payload.source_label,
        "is_dual_source": False,
        "measures": _valid_measures(payload.source_label),
        "measures_label": {"primary": payload.measure, "secondary": None},
        "available_levels": [],
        "direct_competition_count": payload.direct_competition_count,
        "market_size_recent": payload.market_size_recent,
        "market_cagr_5y_pct": payload.market_cagr_5y_pct,
        "is_jw": False,
        "is_target": False,
    }


def period_unit(source: str) -> str:
    """Return the legacy source-specific period unit label."""

    return "월간" if _source_key(source) == "UBIST" else "분기"


def _pct_change(previous: float | None, current: float) -> float | None:
    """Return percentage change with legacy zero-denominator semantics."""

    if previous in (None, 0):
        return None
    return (current - previous) / previous * 100.0


def _previous_year_period(period: str) -> str:
    """Return the same month or quarter in the prior year."""

    year, suffix = period.split("-", 1)
    return f"{int(year) - 1}-{suffix}"


def _expected_periods_per_year(source: str) -> int:
    """Return complete-year period count for the source."""

    return 12 if _source_key(source) == "UBIST" else 4


def _valid_measures(source: str) -> list[str]:
    """Return legacy measure labels for market metadata."""

    if _source_key(source) == "UBIST":
        return ["sales", "volume"]
    return ["counting_unit", "dosage_unit", "sales", "unit"]


def _source_key(source: str) -> str:
    """Normalize API and mart source labels to legacy source families."""

    return "UBIST" if source.strip().upper() == "UBIST" else "IQVIA"
