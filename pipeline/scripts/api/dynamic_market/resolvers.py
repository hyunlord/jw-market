"""Market resolvers convert view-specific filters into brand keys.

Only the general resolver is mounted in the MVP route.  The strategic resolver
stub intentionally implements the same output contract so future overlay logic
can be added without changing aggregation or response serialization.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pipeline.etl.io.mart.molecule_normalize import split_molecule_components
from pipeline.scripts.api import db
from pipeline.scripts.api.dynamic_market.types import (
    BrandRef,
    DynamicMarketInputError,
    MarketDefinition,
    quote_identifier,
)


VALID_MEASURES_BY_SOURCE: dict[str, frozenset[str]] = {
    "ubist": frozenset({"sales", "volume"}),
    "iqvia_nsa": frozenset({"sales", "unit", "dosage_unit", "counting_unit"}),
}


class MarketResolver(Protocol):
    """Resolver protocol shared by general and future strategic views."""

    def resolve(
        self,
        *,
        atc4: list[str],
        molecule: list[str],
        source: str,
        measure: str,
    ) -> MarketDefinition:
        """Return the brand set matching view-specific filters."""


@dataclass(frozen=True, slots=True)
class GeneralViewResolver:
    """Resolve general-view markets from ATC4 and molecule bridge filters."""

    mart_db: str
    bridge_db: str

    def resolve(
        self,
        *,
        atc4: list[str],
        molecule: list[str],
        source: str,
        measure: str,
    ) -> MarketDefinition:
        normalized_source = normalize_source(source)
        normalized_measure = normalize_measure(normalized_source, measure)
        normalized_atc4 = normalize_atc4_list(atc4)
        normalized_molecules = normalize_molecule_list(molecule)
        if not normalized_atc4 and not normalized_molecules:
            raise DynamicMarketInputError("at least one ATC4 or molecule filter is required")

        brands = self._resolve_brands(
            atc4=normalized_atc4,
            molecules=normalized_molecules,
            source=normalized_source,
            measure=normalized_measure,
        )
        return MarketDefinition(
            view="general",
            filter_echo={
                "view": "general",
                "atc4": list(normalized_atc4),
                "molecule": list(molecule),
                "normalized_molecule": list(normalized_molecules),
                "source": normalized_source,
                "measure": normalized_measure,
            },
            source=normalized_source,
            measure=normalized_measure,
            normalized_molecules=normalized_molecules,
            brands=brands,
        )

    def _resolve_brands(
        self,
        *,
        atc4: tuple[str, ...],
        molecules: tuple[str, ...],
        source: str,
        measure: str,
    ) -> tuple[BrandRef, ...]:
        mart_db = quote_identifier(self.mart_db)
        where = ["source = %s", "measure = %s"]
        params: list[str] = [source, measure]
        if atc4:
            where.append(f"atc4_code IN ({placeholders(atc4)})")
            params.extend(atc4)
        if molecules:
            molecule_brand_keys = self._brand_keys_for_molecules(molecules, atc4=atc4, source=source)
            if not molecule_brand_keys:
                return ()
            where.append(f"brand_key IN ({placeholders(molecule_brand_keys)})")
            params.extend(molecule_brand_keys)
        rows = db.fetch_all(
            f"""
            SELECT DISTINCT brand_key, brand_name, atc4_code
            FROM {mart_db}.mart_general_brand_metric
            WHERE {" AND ".join(where)}
            ORDER BY atc4_code, brand_name, brand_key
            """,
            params,
        )
        return tuple(
            BrandRef(
                brand_key=str(row["brand_key"]),
                brand_name=str(row["brand_name"]),
                atc4_code=str(row["atc4_code"]),
            )
            for row in rows
        )

    def _brand_keys_for_molecules(self, molecules: tuple[str, ...], *, atc4: tuple[str, ...], source: str) -> tuple[str, ...]:
        bridge_db = quote_identifier(self.bridge_db)
        where = [
            f"molecule_norm IN ({placeholders(molecules)})",
            "mart_source IN (%s, %s)",
        ]
        params: list[str] = [*molecules, "any", source]
        if atc4:
            where.append(f"atc4_code IN ({placeholders(atc4)})")
            params.extend(atc4)
        rows = db.fetch_all(
            f"""
            SELECT DISTINCT brand_key
            FROM {bridge_db}.mart_brand_molecule
            WHERE {" AND ".join(where)}
            ORDER BY brand_key
            """,
            params,
        )
        return tuple(str(row["brand_key"]) for row in rows)


@dataclass(frozen=True, slots=True)
class StrategicViewResolver:
    """Stub resolver proving strategic overlays can reuse aggregation.

    The next stage should replace the predefined ``ml_id`` lookup with the full
    strategy overlay/cd_filter logic.  It still returns ``MarketDefinition``, so
    the same ``MetricAggregator`` and ``ResponseComposer`` remain untouched.
    """

    mart_db: str
    ml_id: str = "ml_003"

    def resolve(
        self,
        *,
        atc4: list[str],
        molecule: list[str],
        source: str,
        measure: str,
    ) -> MarketDefinition:
        del atc4, molecule
        normalized_source = normalize_source(source)
        normalized_measure = normalize_measure(normalized_source, measure)
        rows = db.fetch_all(
            f"""
            SELECT DISTINCT brand_key, brand_name, '' AS atc4_code
            FROM {quote_identifier(self.mart_db)}.mart_strategic_ml_brand_metric
            WHERE ml_id = %s AND source = %s AND measure = %s
            ORDER BY brand_name, brand_key
            """,
            (self.ml_id, normalized_source, normalized_measure),
        )
        brands = tuple(
            BrandRef(str(row["brand_key"]), str(row["brand_name"]), str(row["atc4_code"]))
            for row in rows
        )
        return MarketDefinition(
            view="strategic_stub",
            filter_echo={"view": "strategic_stub", "ml_id": self.ml_id, "source": normalized_source, "measure": normalized_measure},
            source=normalized_source,
            measure=normalized_measure,
            brands=brands,
        )


def placeholders(values: tuple[str, ...]) -> str:
    """Return a stable placeholder list for PyMySQL parameter binding."""

    return ", ".join(["%s"] * len(values))


def normalize_source(value: str) -> str:
    """Normalize API source labels to mart source labels."""

    normalized = value.strip().lower()
    aliases = {"iqvia": "iqvia_nsa", "nsa": "iqvia_nsa"}
    normalized = aliases.get(normalized, normalized)
    if normalized not in VALID_MEASURES_BY_SOURCE:
        raise DynamicMarketInputError(f"unsupported source: {value}")
    return normalized


def normalize_measure(source: str, value: str) -> str:
    """Validate a measure against the selected source."""

    measure = value.strip().lower()
    if measure not in VALID_MEASURES_BY_SOURCE[source]:
        raise DynamicMarketInputError(f"unsupported measure for {source}: {value}")
    return measure


def normalize_atc4_list(values: list[str]) -> tuple[str, ...]:
    """Normalize ATC4 codes while preserving caller order."""

    seen: set[str] = set()
    normalized: list[str] = []
    for value in values:
        item = value.strip().upper()
        if item and item not in seen:
            seen.add(item)
            normalized.append(item)
    return tuple(normalized)


def normalize_molecule_list(values: list[str]) -> tuple[str, ...]:
    """Normalize molecule filters with the ETL bridge rules."""

    seen: set[str] = set()
    normalized: list[str] = []
    for value in values:
        for component in split_molecule_components(value):
            if component.norm not in seen:
                seen.add(component.norm)
                normalized.append(component.norm)
    return tuple(normalized)
