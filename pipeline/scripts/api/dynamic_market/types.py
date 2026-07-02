"""Shared typed contracts for dynamic market resolution and aggregation."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Final


IDENTIFIER_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_]+$")
DEFAULT_TOP_N: Final[int] = 20
MAX_TOP_N: Final[int] = 100


class DynamicMarketInputError(Exception):
    """Raised when a dynamic-market request cannot be parsed safely."""


@dataclass(frozen=True, slots=True)
class PeriodRange:
    """Inclusive month range used by the runtime metric window."""

    start: str | None = None
    end: str | None = None


@dataclass(frozen=True, slots=True)
class BrandRef:
    """One resolved brand in a dynamic market definition."""

    brand_key: str
    brand_name: str
    atc4_code: str


@dataclass(frozen=True, slots=True)
class DimensionFilter:
    """One normalized product-level sidecar filter.

    Values are normalized with the same rules as the ETL sidecar builder. A
    single ``DimensionFilter`` is an OR set; multiple filters are ANDed by the
    sidecar aggregation path.
    """

    dimension_type: str
    values: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MarketDefinition:
    """Resolved market scope produced by a view-specific resolver."""

    view: str
    filter_echo: dict[str, object]
    source: str
    measure: str
    normalized_molecules: tuple[str, ...] = ()
    brands: tuple[BrandRef, ...] = ()
    dimension_filters: tuple[DimensionFilter, ...] = ()
    focus_brand_key: str | None = None
    strategic_market_kind: str | None = None
    strategic_market_id: str | None = None


@dataclass(frozen=True, slots=True)
class BrandMetric:
    """Aggregated metric values for one brand over the selected period range."""

    brand_key: str
    brand_name: str
    atc4_code: str
    total_value: float
    market_share_pct: float
    rank: int
    latest_period: str | None
    latest_value: float | None
    monthly_series: tuple[dict[str, float | str], ...] = field(default_factory=tuple)
    ubist_channel_by_display: dict[str, dict[str, float]] = field(default_factory=dict)
    ubist_channel_by_code: dict[str, dict[str, float]] = field(default_factory=dict)
    channel_specialty_matrix: dict[str, dict[str, dict[str, float]]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AggregatedMetrics:
    """View-agnostic metric result computed from brand rows."""

    source: str
    measure: str
    unit_label: str
    market_size: float
    hhi: float | None
    cagr: float | None
    monthly_series: tuple[dict[str, float | str], ...]
    brands: tuple[BrandMetric, ...]
    all_brands: tuple[BrandMetric, ...] = ()


def quote_identifier(name: str) -> str:
    """Return a safe SQL identifier for database/table qualification.

    Database names arrive from deployment env vars.  They cannot be parameterized
    as SQL values, so this function permits only MySQL identifier characters
    before wrapping the name in backticks.
    """

    if not IDENTIFIER_RE.fullmatch(name):
        raise DynamicMarketInputError(f"unsafe SQL identifier: {name}")
    return f"`{name}`"


def clamp_top_n(value: int | None) -> int:
    """Clamp response size to the same operator-safe range as market-status."""

    if value is None:
        return DEFAULT_TOP_N
    return max(1, min(int(value), MAX_TOP_N))
