from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class SourceMetric(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value_recent: float | None = None
    ms_recent_pct: float | None = None
    gr_mom_pct: float | None = None
    gr_qoq_pct: float | None = None
    gr_yoy_pct: float | None = None
    gr_yoy_mat_pct: float | None = None
    gr_yoy_ym_pct: float | None = None


class FrontMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value_recent: float | None = None
    ms_recent_pct: float | None = None
    gr_qoq_pct: float | None = None
    gr_yoy_pct: float | None = None
    ms_change_yoy_pct: float | None = None
    gr_mom_pct: float | None = None
    gr_yoy_mat_pct: float | None = None
    gr_yoy_ym_pct: float | None = None
    sources_data: dict[str, SourceMetric]
    default_source: str


class BackMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cagr_5y_pct: float | None = None
    sales_first_period_krw: float | None = None
    ms_first_period_pct: float | None = None
    period_first: str | None = None


class BackExtendedMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market_size_recent: float | None = None
    market_cagr_5y_pct: float | None = None
    brand_cagr_5y_pct: float | None = None
    excess_growth_pct: float | None = None
    source_label: str
    is_dual_source: bool
    sources: list[str]
    market_definition_label: str
    market_definition_full: str
    atc_count: int
    direct_competition_count: int | None = None
    market_label_kor: str


class MarketStatusCard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rank: int
    brand: str
    company: str
    is_jw: bool
    is_target: bool
    market_id: str
    market_name: str
    market_name_short: str
    market_label_kor: str
    mkt_team: str | None = None
    atc_codes: list[str]
    atc_desc: str
    sources: list[str]
    nhi_type: str
    front: FrontMetrics
    back: BackMetrics
    back_extended: BackExtendedMetrics


class MarketStatusBrand(BaseModel):
    brand_id: str
    brand_name: str
    is_jw: bool
    market_share: float | None = None
    rank: int | None = None
    cagr_5y: float | None = None
    ei_5y: float | None = None


class MarketStatusMl(BaseModel):
    ml_id: str
    ml_name: str | None = None
    period_yyyymm: str
    hhi: float | None = None
    market_cagr_5y: float | None = None
    top_brands: list[MarketStatusBrand]
    total_brands: int


class MarketStatusResponse(BaseModel):
    period: str
    markets: list[MarketStatusMl]
    total_markets: int
    generated_at: str
