from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class MarketScopeKind(StrEnum):
    STRATEGIC = "strategic"
    GENERAL_ATC4 = "general_atc4"
    GENERAL_COMPOSITE = "general_composite"


@dataclass(frozen=True, slots=True)
class MarketScope:
    kind: MarketScopeKind
    market_id: str | None = None
    atc4: tuple[str, ...] = ()
    filters: tuple[tuple[str, tuple[str, ...]], ...] = ()


@dataclass(frozen=True, slots=True)
class ScopeResolution:
    scope: MarketScope
    source: str
    normalized_arguments: dict[str, object]
    normalizations: tuple[str, ...] = ()
    fallback_reason: str | None = None


class MarketScopeResolutionError(LookupError):
    reason_code = "market_scope_error"

    def __init__(self, message: str) -> None:
        super().__init__(f"{self.reason_code}: {message}")


class NoStrategicMembershipError(MarketScopeResolutionError):
    reason_code = "no_strategic_membership"


class UnknownBrandError(MarketScopeResolutionError):
    reason_code = "unknown_brand"


class InvalidMarketLabelError(MarketScopeResolutionError):
    reason_code = "invalid_market_label"


class UnsupportedSourceError(MarketScopeResolutionError):
    reason_code = "unsupported_source"


class AmbiguousFamilyError(MarketScopeResolutionError):
    reason_code = "ambiguous_family"


class AmbiguousMarketError(MarketScopeResolutionError):
    reason_code = "ambiguous_market"


class NoAnchorError(MarketScopeResolutionError):
    reason_code = "no_anchor"


class GeneralCompositeUnavailableError(MarketScopeResolutionError):
    reason_code = "general_composite_unavailable"


class GeneralMetricUnavailableError(MarketScopeResolutionError):
    reason_code = "general_metric_unavailable"


class BrandOutsideCompositeScopeError(MarketScopeResolutionError):
    reason_code = "brand_outside_composite_scope"
