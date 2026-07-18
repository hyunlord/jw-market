from __future__ import annotations

from dataclasses import dataclass


BrandKey = tuple[str, str, str, str, str]


@dataclass(frozen=True, slots=True)
class DerivedBrandPoint:
    value_krw: float | None
    share_pct: float | None
    rank: int | None
    source_status: str


@dataclass(frozen=True, slots=True)
class DerivedMarketPoint:
    total_krw: float | None
    hhi: float | None
    cr5_pct: float | None
    denominator: int


@dataclass(frozen=True, slots=True)
class DerivedCompetitor:
    brand: str
    rank_start: int | None
    rank_end: int
    sales_start_krw: float | None
    sales_end_krw: float
    share_start_pct: float | None
    share_end_pct: float | None


@dataclass(frozen=True, slots=True)
class DerivedBrandInsight:
    periods: tuple[str, ...]
    missing_periods: tuple[str, ...]
    share_start_pct: float | None
    share_end_pct: float | None
    share_delta_pctp: float | None
    sales_start_krw: float | None
    sales_end_krw: float | None
    sales_delta_krw: float | None
    market_start_krw: float | None
    market_end_krw: float | None
    brand_growth_pct: float | None
    market_growth_pct: float | None
    excess_growth_pctp: float | None
    brand_mom_pct: float | None
    market_mom_pct: float | None
    brand_yoy_pct: float | None
    market_yoy_pct: float | None
    brand_cmgr_pct: float | None
    market_cmgr_pct: float | None
    brand_cqgr_pct: float | None
    market_cqgr_pct: float | None
    rank_start: int | None
    rank_end: int | None
    share_max_pct: float | None
    share_max_period: str | None
    share_min_pct: float | None
    share_min_period: str | None
    turning_point: str | None
    turning_kind: str | None
    trend_direction: str | None
    trend_months: int
    hhi_end: float | None
    cr5_end_pct: float | None
    denominator_end: int
    competitors: tuple[DerivedCompetitor, ...]


@dataclass(frozen=True, slots=True)
class DerivedParityReport:
    classification: str
    checked: int
    population: int
    failures: tuple[str, ...]
    exit_code: int
