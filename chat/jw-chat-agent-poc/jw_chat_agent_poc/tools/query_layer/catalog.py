from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from jw_chat_agent_poc.tools.query_layer.store import MartSnapshot


METRICS: Final[tuple[str, ...]] = ("sales", "share", "rank", "hhi", "growth")
DERIVATIONS: Final[tuple[str, ...]] = ("share", "hhi", "growth", "rank", "delta", "trend", "top_n", "mix", "gap", "yoy", "average")
SORTS: Final[tuple[str, ...]] = ("sales_desc", "share_desc", "rank_asc", "period_asc")
BASE_DIMENSIONS: Final[tuple[str, ...]] = (
    "channel",
    "specialty",
    "molecule",
    "dosage_form",
    "nhi_type",
    "ox_gx",
    "product",
    "company",
)


@dataclass(frozen=True, slots=True)
class QueryCatalog:
    """Market-specific enum catalog injected into tool schemas."""

    market: str
    source: str
    view: str
    dimensions: tuple[str, ...]
    group_by: tuple[str, ...]
    metrics: tuple[str, ...] = METRICS
    derivations: tuple[str, ...] = DERIVATIONS
    sorts: tuple[str, ...] = SORTS

    @classmethod
    def from_snapshot(cls, snapshot: MartSnapshot, market: str, source: str = "ubist") -> "QueryCatalog":
        records = snapshot.market_records(market, source, "sales")
        available: list[str] = []
        if any(record.channel_data for record in records):
            available.append("channel")
        if any(record.specialty_data for record in records):
            available.append("specialty")
        if any(record.molecule() for record in records):
            available.append("molecule")
        if any(record.dosage_form() or record.class_label() for record in records):
            available.append("dosage_form")
        if any(record.nhi_type() for record in records):
            available.append("nhi_type")
        if any(record.ox_gx() for record in records):
            available.append("ox_gx")
        available.extend(("product", "company"))
        dimensions = tuple(item for item in BASE_DIMENSIONS if item in set(available))
        return cls(market=market, source=source, view="market_landscape", dimensions=dimensions, group_by=(*dimensions, "period"))

    def schema_fragment(self) -> dict[str, tuple[str, ...]]:
        return {
            "dimensions": self.dimensions,
            "group_by": self.group_by,
            "metrics": self.metrics,
            "derive": self.derivations,
            "sort": self.sorts,
            "source": (self.source,),
            "view": (self.view,),
            "market": (self.market,),
        }


def default_catalog() -> QueryCatalog:
    """Fallback enum catalog used before a brand-bound mart snapshot is loaded."""

    dimensions = ("channel", "specialty", "molecule", "dosage_form", "ox_gx", "product", "company")
    return QueryCatalog(
        market="ml_006",
        source="ubist",
        view="market_landscape",
        dimensions=dimensions,
        group_by=(*dimensions, "period"),
    )
