from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from jw_chat_agent_poc.tools.query_layer.market_structure import CLASS_2_KEY, structure_from_records
from jw_chat_agent_poc.tools.query_layer.store import MartRecord, MartSnapshot


PRESCRIPTION_VOLUME_METRIC: Final = "prescription_volume"
METRICS: Final[tuple[str, ...]] = (
    "sales",
    "share",
    "rank",
    "hhi",
    "growth",
    PRESCRIPTION_VOLUME_METRIC,
)
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
class MetricDefinition:
    public_name: str
    measure: str
    display_name: str
    unit_label: str
    sources: tuple[str, ...]


_METRIC_DEFINITIONS: Final[dict[str, MetricDefinition]] = {
    "sales": MetricDefinition("sales", "sales", "매출", "KRW", ("ubist", "iqvia_nsa")),
    "share": MetricDefinition("share", "sales", "점유율", "%", ("ubist", "iqvia_nsa")),
    "market_share": MetricDefinition("market_share", "sales", "점유율", "%", ("ubist", "iqvia_nsa")),
    "rank": MetricDefinition("rank", "sales", "순위", "위", ("ubist", "iqvia_nsa")),
    "series": MetricDefinition("series", "sales", "매출", "KRW", ("ubist", "iqvia_nsa")),
    "trend": MetricDefinition("series", "sales", "매출", "KRW", ("ubist", "iqvia_nsa")),
    "hhi": MetricDefinition("hhi", "sales", "HHI", "index", ("ubist", "iqvia_nsa")),
    "growth": MetricDefinition("growth", "sales", "성장률", "%", ("ubist", "iqvia_nsa")),
    "momentum": MetricDefinition("momentum", "sales", "모멘텀", "score", ("ubist", "iqvia_nsa")),
    "ei": MetricDefinition("ei", "sales", "EI", "index", ("ubist", "iqvia_nsa")),
    "growth_contribution": MetricDefinition("growth_contribution", "sales", "성장기여", "%", ("ubist", "iqvia_nsa")),
    PRESCRIPTION_VOLUME_METRIC: MetricDefinition(
        PRESCRIPTION_VOLUME_METRIC,
        "volume",
        "처방량",
        "Rx",
        ("ubist",),
    ),
}


def metric_definition(metric: str) -> MetricDefinition:
    key = str(metric or "").strip().casefold()
    if not key:
        raise ValueError("metric is required; blank metric cannot default to sales")
    try:
        return _METRIC_DEFINITIONS[key]
    except KeyError as exc:
        raise ValueError(f"unsupported metric: {metric}") from exc


def measure_for_metrics(metrics: tuple[str, ...] | list[str]) -> str:
    if not metrics:
        raise ValueError("metrics is required; blank metrics cannot default to sales")
    measures = {metric_definition(metric).measure for metric in metrics}
    if len(measures) != 1:
        raise ValueError("cannot mix metrics from more than one measure in a single query")
    return next(iter(measures))


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
        metrics = METRICS[:-1]
        if snapshot.market_records(market, "ubist", "volume"):
            metrics = METRICS
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
        elif any(record.dosage_form() for record in records):
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
            metrics=metrics,
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
    """Shape-only catalog used before a market-bound mart snapshot is loaded."""

    dimensions = ("channel", "specialty", "molecule", "dosage_form", "ox_gx", "product", "company")
    return QueryCatalog(
        market="",
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
