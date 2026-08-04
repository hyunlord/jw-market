from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from jw_chat_agent_poc.tools.general_view_backend import AtcCandidate, GeneralMarket


class GeneralMarketBackend(Protocol):
    def candidates(self, brand: str, source: str) -> tuple[AtcCandidate, ...]: ...

    def market(
        self,
        atc4: str,
        brand: str | None,
        source: str,
        measure: str,
    ) -> GeneralMarket: ...


class StrategicMarketBackend(Protocol):
    def brand_memberships(self) -> Sequence[Mapping[str, str]]: ...

    def brand_metric(
        self,
        brand: str,
        metric: str,
        period: str,
        market: str | None = None,
        source: str = "",
        history_points: int = 10,
    ) -> dict[str, Any]: ...

    def market_scope(self, brand: str, market: str | None = None) -> dict[str, Any]: ...

    def dimension_breakdown(
        self,
        brand: str,
        dimension: str,
        source: str = "",
        period: str = "latest",
        limit: int = 10,
        market: str | None = None,
        metric: str = "sales",
    ) -> dict[str, Any]: ...

    def market_member_metric(
        self,
        brand: str,
        comparison: str,
        market: str | None = None,
        metric: str = "series",
    ) -> dict[str, Any]: ...
