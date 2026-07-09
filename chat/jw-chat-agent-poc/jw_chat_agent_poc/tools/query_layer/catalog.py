from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from jw_chat_agent_poc.tools.query_layer.market_structure import CLASS_2_KEY, structure_from_records
from jw_chat_agent_poc.tools.query_layer.store import MartRecord, MartSnapshot


METRICS: Final[tuple[str, ...]] = ("sales", "share", "rank", "hhi", "growth")
DERIVATIONS: Final[tuple[str, ...]] = ("share", "hhi", "growth", "rank", "delta", "trend", "top_n", "mix", "gap", "yoy", "average")
SORTS: Final[tuple[str, ...]] = ("sales_desc", "share_desc", "rank_asc", "period_asc")
BASE_DIMENSIONS: Final[tuple[str, ...]] = (
    "channel",
    "specialty",
    "molecule",
    "class_1",
    "class_2",
    "dosage_form",
    "nhi_type",
    "ox_gx",
    "manufacturer",
    "fish_oil",
    "strength_pack",
    "product",
    "company",
)
METADATA_DIMENSIONS: Final[frozenset[str]] = frozenset(
    {
        "atc4_code",
        "atc4_desc",
        "catalog_brand_id",
        "catalog_status",
        "class",
        "products",
        "raw_company",
    }
)


@dataclass(frozen=True, slots=True)
class QueryCatalog:
    """Market-specific enum catalog injected into tool schemas."""

    market: str
    source: str
    sources: tuple[str, ...]
    view: str
    dimensions: tuple[str, ...]
    group_by: tuple[str, ...]
    metrics: tuple[str, ...] = METRICS
    derivations: tuple[str, ...] = DERIVATIONS
    sorts: tuple[str, ...] = SORTS
    market_structure: dict[str, object] | None = None

    @classmethod
    def from_snapshot(cls, snapshot: MartSnapshot, market: str, source: str = "ubist") -> "QueryCatalog":
        records = snapshot.market_records(market, source, "sales")
        structure = structure_from_records(records)
        sources = snapshot.sources_for_market(market)
        available: list[str] = []
        if any(record.channel_data for record in records):
            available.append("channel")
        if any(record.specialty_data for record in records):
            available.append("specialty")
        if any(record.molecule() for record in records):
            available.append("molecule")
        if structure.get("display_axis") == CLASS_2_KEY:
            available.append(CLASS_2_KEY)
        elif any(record.dosage_form() or record.class_label() for record in records):
            available.append("dosage_form")
        dynamic_dimensions = _business_dimensions(records, structure)
        available.extend(dynamic_dimensions)
        available.extend(("product", "company"))
        dimensions = tuple(item for item in BASE_DIMENSIONS if item in set(available))
        return cls(
            market=market,
            source=source,
            sources=sources or (source,),
            view="market_landscape",
            dimensions=dimensions,
            group_by=(*dimensions, "period"),
            market_structure=structure or None,
        )

    def schema_fragment(self) -> dict[str, tuple[str, ...]]:
        return {
            "dimensions": self.dimensions,
            "group_by": self.group_by,
            "metrics": self.metrics,
            "derive": self.derivations,
            "sort": self.sorts,
            "source": self.sources or (self.source,),
            "view": (self.view,),
            "market": (self.market,),
        }


def default_catalog() -> QueryCatalog:
    """Fallback enum catalog used before a brand-bound mart snapshot is loaded."""

    dimensions = ("channel", "specialty", "molecule", "dosage_form", "ox_gx", "product", "company")
    return QueryCatalog(
        market="ml_006",
        source="ubist",
        sources=("ubist",),
        view="market_landscape",
        dimensions=dimensions,
        group_by=(*dimensions, "period"),
    )


def _business_dimensions(records: tuple[MartRecord, ...], structure: dict[str, object]) -> tuple[str, ...]:
    display_axis = str(structure.get("display_axis") or "")
    values: list[str] = []
    for record in records:
        for key, raw in record.by_dimension.items():
            if key in METADATA_DIMENSIONS or (key == "class_1" and display_axis == CLASS_2_KEY):
                continue
            if raw not in {None, ""} and key not in values:
                values.append(str(key))
    return tuple(values)
