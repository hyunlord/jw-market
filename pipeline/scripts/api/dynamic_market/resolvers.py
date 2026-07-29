"""Market resolvers convert view-specific filters into brand keys.

Only the general resolver is mounted in the MVP route.  The strategic resolver
stub intentionally implements the same output contract so future overlay logic
can be added without changing aggregation or response serialization.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pipeline.contracts.dimension_registry import (
    DIMENSION_REGISTRY,
    api_dimension_names,
    normalize_dimension_value,
)
from pipeline.contracts.serving_tables import (
    GENERAL_FILTER_DIMENSION_TABLE as FILTER_DIMENSION_TABLE,
    STRATEGIC_FILTER_DIMENSION_TABLE as STRATEGIC_DIMENSION_TABLE,
)
from pipeline.domain.molecules import split_molecule_components
from pipeline.scripts.api import db
from pipeline.scripts.api.catalog import get_display_brand
from pipeline.scripts.api.dynamic_market.channel_axis import ChannelAxisFilter
from pipeline.scripts.api.dynamic_market.types import (
    BrandRef,
    DimensionFilter,
    DynamicMarketInputError,
    MarketDefinition,
    quote_identifier,
)
from pipeline.scripts.utils.atc4 import atc4_source_aliases


VALID_MEASURES_BY_SOURCE: dict[str, frozenset[str]] = {
    "ubist": frozenset({"sales", "volume"}),
    "iqvia_nsa": frozenset({"sales", "unit", "dosage_unit", "counting_unit"}),
}
EXACT_MATCH_DIMENSIONS: frozenset[str] = frozenset({"pack"})


class MarketResolver(Protocol):
    """Resolver protocol shared by general and future strategic views."""

    def resolve(
        self,
        *,
        atc4: list[str],
        molecule: list[str],
        analysis_level: dict[str, dict[str, list[str]]] | None,
        channel_axis: ChannelAxisFilter | None,
        focus_brand_key: str | None,
        source: str,
        measure: str,
    ) -> MarketDefinition:
        """Return the brand set matching view-specific filters."""


@dataclass(frozen=True, slots=True)
class GeneralViewResolver:
    """Resolve general-view markets from ATC4 and enabled source dimensions."""

    mart_db: str
    bridge_db: str

    def resolve(
        self,
        *,
        atc4: list[str],
        molecule: list[str],
        analysis_level: dict[str, dict[str, list[str]]] | None = None,
        channel_axis: ChannelAxisFilter | None = None,
        focus_brand_key: str | None = None,
        source: str,
        measure: str,
    ) -> MarketDefinition:
        normalized_source = normalize_source(source)
        normalized_measure = normalize_measure(normalized_source, measure)
        normalized_atc4 = normalize_atc4_list(atc4)
        normalized_focus_brand = focus_brand_key.strip() if focus_brand_key else None
        if molecule:
            raise DynamicMarketInputError("molecule filters are disabled for D-1; use ATC4 and enabled source-specific dimensions")
        normalized_molecules = normalize_molecule_list(molecule)
        dimension_filters = build_dimension_filters(
            analysis_level=analysis_level or {},
            source=normalized_source,
        )
        if not normalized_atc4 and normalized_focus_brand:
            normalized_atc4 = self._default_atc4_for_focus(
                focus_brand_key=normalized_focus_brand,
                source=normalized_source,
                measure=normalized_measure,
            )
        if not normalized_atc4 and not normalized_molecules:
            raise DynamicMarketInputError("at least one ATC4 or molecule filter is required")

        query_atc4 = expand_atc4_for_source(normalized_atc4, source=normalized_source)
        brands = self._resolve_brands(
            atc4=query_atc4,
            molecules=normalized_molecules,
            source=normalized_source,
            measure=normalized_measure,
        )
        if not brands:
            raise DynamicMarketInputError("general market rows were not found for the requested ATC4/molecule filters")
        if dimension_filters and normalized_focus_brand:
            dimension_filters = self._with_focus_dimension_values(
                filters=dimension_filters,
                focus_brand_key=normalized_focus_brand,
                brands=brands,
                source=normalized_source,
                measure=normalized_measure,
            )
        filter_echo = {
            "view": "general",
            "atc4": list(normalized_atc4),
            "molecule": list(molecule),
            "normalized_molecule": list(normalized_molecules),
            "analysis_level": _dimension_echo(dimension_filters),
            "focus_brand_key": normalized_focus_brand,
            "source": normalized_source,
            "measure": normalized_measure,
        }
        channel_axis_echo = _channel_axis_echo(channel_axis)
        if channel_axis_echo:
            filter_echo["channel_axis"] = channel_axis_echo
        return MarketDefinition(
            view="general",
            filter_echo=filter_echo,
            source=normalized_source,
            measure=normalized_measure,
            normalized_molecules=normalized_molecules,
            brands=brands,
            dimension_filters=dimension_filters,
            channel_axis=channel_axis,
            focus_brand_key=normalized_focus_brand,
            market_catalog_row=self._market_catalog_row_for_focus(
                focus_brand_key=normalized_focus_brand,
                brands=brands,
            ),
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

    def _default_atc4_for_focus(self, *, focus_brand_key: str, source: str, measure: str) -> tuple[str, ...]:
        """Resolve the general-view default market for a selected brand.

        The D-1 general API keeps the existing ATC4 path as the explicit market
        definition. When a caller only supplies a focus brand, missing ATC4
        means select all ATC4 buckets attached to that brand for the source and
        measure.
        """

        rows = db.fetch_all(
            f"""
            SELECT DISTINCT atc4_code
            FROM {quote_identifier(self.mart_db)}.mart_general_brand_metric
            WHERE brand_key = %s
              AND source = %s
              AND measure = %s
            ORDER BY atc4_code
            """,
            (focus_brand_key, source, measure),
        )
        atc4_codes = tuple(str(row["atc4_code"]) for row in rows if row.get("atc4_code"))
        if not atc4_codes:
            raise DynamicMarketInputError(f"focus brand has no general-view ATC4 for {source}/{measure}: {focus_brand_key}")
        return atc4_codes

    def _market_catalog_row_for_focus(
        self,
        *,
        focus_brand_key: str | None,
        brands: tuple[BrandRef, ...],
    ) -> dict[str, object] | None:
        if not focus_brand_key:
            return None
        candidates = [focus_brand_key]
        requested = focus_brand_key.strip()
        for brand in brands:
            if requested in {brand.brand_key, brand.brand_name}:
                candidates.append(brand.brand_name)
                break
        for candidate in candidates:
            display_brand = get_display_brand(candidate.strip())
            if display_brand is None:
                continue
            row = db.fetch_one(
                """
                SELECT *
                FROM catalog_ml_market
                WHERE ml_id = %s
                LIMIT 1
                """,
                (display_brand.ml_id,),
            )
            return dict(row) if row else None
        return None

    def _dimension_filters(
        self,
        *,
        analysis_level: dict[str, dict[str, list[str]]],
        source: str,
    ) -> tuple[DimensionFilter, ...]:
        source_payload = analysis_level.get(_api_source_key(source), {})
        if _other_source_has_values(analysis_level, source):
            raise DynamicMarketInputError(f"analysis_level must match selected source: {source}")
        registry = DIMENSION_REGISTRY[source]
        api_to_registry = api_dimension_names(source, include_shared=True)
        filters: list[DimensionFilter] = []
        for api_name, values in sorted(source_payload.items()):
            if not values:
                continue
            dimension_type = api_to_registry.get(api_name)
            if dimension_type is None:
                raise DynamicMarketInputError(f"unsupported analysis_level dimension for {source}: {api_name}")
            spec = registry[dimension_type]
            if not spec.enabled:
                raise DynamicMarketInputError(f"analysis_level dimension is disabled for D-1: {api_name}")
            normalized = _normalize_dimension_values(values)
            if normalized:
                filters.append(DimensionFilter(dimension_type=dimension_type, values=normalized))
        return tuple(filters)

    def _with_focus_dimension_values(
        self,
        *,
        filters: tuple[DimensionFilter, ...],
        focus_brand_key: str,
        brands: tuple[BrandRef, ...],
        source: str,
        measure: str,
    ) -> tuple[DimensionFilter, ...]:
        focus_brand = focus_brand_key.strip()
        if not focus_brand:
            return filters
        expandable_filters = tuple(item for item in filters if item.dimension_type not in EXACT_MATCH_DIMENSIONS)
        if not expandable_filters:
            return filters
        scoped_atc4 = tuple(sorted({brand.atc4_code for brand in brands if brand.brand_key == focus_brand and brand.atc4_code}))
        if not scoped_atc4:
            return filters
        mart_db = quote_identifier(self.mart_db)
        rows = db.fetch_all(
            f"""
            SELECT DISTINCT dimension_type, dimension_value_norm
            FROM {mart_db}.{quote_identifier(FILTER_DIMENSION_TABLE)}
            WHERE source = %s
              AND measure = %s
              AND brand_key = %s
              AND atc4_code IN ({placeholders(scoped_atc4)})
              AND dimension_type IN ({placeholders(tuple(item.dimension_type for item in expandable_filters))})
            ORDER BY dimension_type, dimension_value_norm
            """,
            (source, measure, focus_brand, *scoped_atc4, *(item.dimension_type for item in expandable_filters)),
        )
        values_by_dimension: dict[str, set[str]] = {item.dimension_type: set(item.values) for item in filters}
        for row in rows:
            values_by_dimension[str(row["dimension_type"])].add(str(row["dimension_value_norm"]))
        return tuple(
            DimensionFilter(dimension_type=item.dimension_type, values=tuple(sorted(values_by_dimension[item.dimension_type])))
            for item in filters
        )


@dataclass(frozen=True, slots=True)
class StrategicMarketSelection:
    """Internal strategic market id chosen from a focus brand."""

    market_kind: str
    market_id: str


def resolve_strategic_market_for_focus(
    *,
    mart_db: str,
    view_kind: str | None,
    focus_brand_key: str | None,
    source: str,
    measure: str,
) -> StrategicMarketSelection:
    """Resolve a public brand-based strategic request to an internal market id.

    Ambiguous brand membership is intentionally deterministic: use the first
    market id by lexical id order, matching the audit SQL's `ORDER BY` basis.
    """

    market_kind = normalize_strategic_view_kind(view_kind=view_kind, ml_id=None, cd_market_id=None)
    focus_brand = (focus_brand_key or "").strip()
    if not focus_brand:
        raise DynamicMarketInputError("strategic view requires focus_brand_key")
    normalized_source = normalize_source(source)
    normalized_measure = normalize_measure(normalized_source, measure)
    table = "mart_strategic_cd_brand_metric" if market_kind == "cd" else "mart_strategic_ml_brand_metric"
    id_column = "cd_market_id" if market_kind == "cd" else "ml_id"
    rows = db.fetch_all(
        f"""
        SELECT DISTINCT {id_column} AS market_id
        FROM {quote_identifier(mart_db)}.{table}
        WHERE (brand_key = %s OR brand_name = %s)
          AND source = %s
          AND measure = %s
        ORDER BY {id_column}
        """,
        (focus_brand, focus_brand, normalized_source, normalized_measure),
    )
    market_ids = tuple(str(row["market_id"]) for row in rows if row.get("market_id"))
    if not market_ids:
        raise DynamicMarketInputError(
            f"focus brand has no strategic {market_kind} market for {normalized_source}/{normalized_measure}: {focus_brand}"
        )
    return StrategicMarketSelection(market_kind=market_kind, market_id=market_ids[0])


@dataclass(frozen=True, slots=True)
class StrategicViewResolver:
    mart_db: str
    dimension_db: str | None = None

    def resolve(
        self,
        *,
        view_kind: str | None = None,
        ml_id: str | None = None,
        cd_market_id: str | None = None,
        atc4: list[str],
        molecule: list[str],
        analysis_level: dict[str, dict[str, list[str]]] | None = None,
        channel_axis: ChannelAxisFilter | None = None,
        focus_brand_key: str | None = None,
        source: str,
        measure: str,
    ) -> MarketDefinition:
        if molecule:
            raise DynamicMarketInputError("strategic view accepts only top-level ATC4 narrowing, not molecule expansion")
        if channel_axis and channel_axis.is_active:
            raise DynamicMarketInputError("channel_axis is supported only for general views")
        normalized_source = normalize_source(source)
        normalized_measure = normalize_measure(normalized_source, measure)
        market_kind = normalize_strategic_view_kind(view_kind=view_kind, ml_id=ml_id, cd_market_id=cd_market_id)
        market_id = normalize_strategic_market_id(market_kind=market_kind, ml_id=ml_id, cd_market_id=cd_market_id)
        table = "mart_strategic_ml_brand_metric" if market_kind == "ml" else "mart_strategic_cd_brand_metric"
        id_column = "ml_id" if market_kind == "ml" else "cd_market_id"
        rows = db.fetch_all(
            f"""
            SELECT DISTINCT brand_key, brand_name, '' AS atc4_code
            FROM {quote_identifier(self.mart_db)}.{table}
            WHERE {id_column} = %s AND source = %s AND measure = %s
            ORDER BY brand_name, brand_key
            """,
            (market_id, normalized_source, normalized_measure),
        )
        brands = tuple(
            BrandRef(str(row["brand_key"]), str(row["brand_name"]), str(row["atc4_code"]))
            for row in rows
        )
        filters = build_dimension_filters(
            analysis_level=_with_strategic_atc4(analysis_level=analysis_level or {}, source=normalized_source, atc4=atc4),
            source=normalized_source,
        )
        view = "strategic_ml" if market_kind == "ml" else "strategic_cd"
        echo: dict[str, object] = {
            "view": view,
            "view_kind": "market_landscape" if market_kind == "ml" else "competitive_dynamics",
            "source": normalized_source,
            "measure": normalized_measure,
            "analysis_level": _dimension_echo(filters),
            "focus_brand_key": focus_brand_key,
        }
        if market_kind == "ml":
            echo["ml_id"] = market_id
        else:
            echo["cd_market_id"] = market_id
        return MarketDefinition(
            view=view,
            filter_echo=echo,
            source=normalized_source,
            measure=normalized_measure,
            brands=brands,
            dimension_filters=filters,
            focus_brand_key=focus_brand_key,
            strategic_market_kind=market_kind,
            strategic_market_id=market_id,
            market_catalog_row=self._market_catalog_row(market_kind=market_kind, market_id=market_id),
        )

    def _market_catalog_row(self, *, market_kind: str, market_id: str) -> dict[str, object] | None:
        table = "catalog_cd_market" if market_kind == "cd" else "catalog_ml_market"
        id_column = "cd_id" if market_kind == "cd" else "ml_id"
        row = db.fetch_one(
            f"""
            SELECT *
            FROM {table}
            WHERE {id_column} = %s
            LIMIT 1
            """,
            (market_id,),
        )
        if not row:
            return None
        record = dict(row)
        if market_kind == "cd":
            record["cd_market_id"] = record.get("cd_id")
        return record


def placeholders(values: tuple[str, ...]) -> str:
    """Return a stable placeholder list for PyMySQL parameter binding."""

    return ", ".join(["%s"] * len(values))


def build_dimension_filters(
    *,
    analysis_level: dict[str, dict[str, list[str]]],
    source: str,
) -> tuple[DimensionFilter, ...]:
    source_payload = analysis_level.get(_api_source_key(source), {})
    if _other_source_has_values(analysis_level, source):
        raise DynamicMarketInputError(f"analysis_level must match selected source: {source}")
    registry = DIMENSION_REGISTRY[source]
    api_to_registry = api_dimension_names(source, include_shared=True)
    filters: list[DimensionFilter] = []
    for api_name, values in sorted(source_payload.items()):
        if not values:
            continue
        dimension_type = api_to_registry.get(api_name)
        if dimension_type is None:
            raise DynamicMarketInputError(f"unsupported analysis_level dimension for {source}: {api_name}")
        spec = registry[dimension_type]
        if not spec.enabled:
            raise DynamicMarketInputError(f"analysis_level dimension is disabled for dynamic filters: {api_name}")
        normalized = _normalize_dimension_values(values)
        if normalized:
            filters.append(DimensionFilter(dimension_type=dimension_type, values=normalized))
    return tuple(filters)


def _with_strategic_atc4(
    *,
    analysis_level: dict[str, dict[str, list[str]]],
    source: str,
    atc4: list[str],
) -> dict[str, dict[str, list[str]]]:
    """Attach public top-level strategic ATC4 narrowing to the internal dimension payload."""

    if not atc4:
        return analysis_level
    source_key = _api_source_key(source)
    merged = {
        key: {inner_key: list(values) for inner_key, values in payload.items()}
        for key, payload in analysis_level.items()
    }
    merged.setdefault(source_key, {})["atc4"] = list(atc4)
    return merged


def normalize_strategic_view_kind(*, view_kind: str | None, ml_id: str | None, cd_market_id: str | None) -> str:
    if view_kind:
        normalized = view_kind.strip().lower()
        if normalized in {"market_landscape", "strategic_ml", "ml"}:
            return "ml"
        if normalized in {"competitive_dynamics", "strategic_cd", "cd"}:
            return "cd"
        raise DynamicMarketInputError(f"unsupported strategic view_kind: {view_kind}")
    if ml_id and not cd_market_id:
        return "ml"
    if cd_market_id and not ml_id:
        return "cd"
    raise DynamicMarketInputError("strategic view requires view_kind plus ml_id or cd_market_id")


def normalize_strategic_market_id(*, market_kind: str, ml_id: str | None, cd_market_id: str | None) -> str:
    market_id = ml_id if market_kind == "ml" else cd_market_id
    label = "ml_id" if market_kind == "ml" else "cd_market_id"
    if not market_id:
        raise DynamicMarketInputError(f"strategic {market_kind} view requires {label}")
    return market_id.strip()


def _api_source_key(source: str) -> str:
    return "iqvia" if source == "iqvia_nsa" else source


def _other_source_has_values(analysis_level: dict[str, dict[str, list[str]]], source: str) -> bool:
    allowed = _api_source_key(source)
    for source_key, payload in analysis_level.items():
        if source_key == allowed:
            continue
        if any(values for values in payload.values()):
            return True
    return False


def _normalize_dimension_values(values: list[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = normalize_dimension_value(value)
        if item and item not in seen:
            seen.add(item)
            normalized.append(item)
    return tuple(normalized)


def _dimension_echo(filters: tuple[DimensionFilter, ...]) -> dict[str, list[str]]:
    return {item.dimension_type: list(item.values) for item in filters}


def _channel_axis_echo(channel_axis: ChannelAxisFilter | None) -> dict[str, object]:
    if channel_axis is None or not channel_axis.is_active:
        return {}
    if channel_axis.source == "iqvia_nsa":
        return {
            "source": channel_axis.source,
            "audit_code": list(channel_axis.audit_codes),
        }
    return {
        "source": channel_axis.source,
        "facility": list(channel_axis.facilities),
        "specialty": list(channel_axis.specialties),
        "pairs": [
            {"facility": item.facility, "specialty": item.specialty}
            for item in channel_axis.pairs
        ],
    }


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


def expand_atc4_for_source(values: tuple[str, ...], *, source: str) -> tuple[str, ...]:
    """Expand canonical ATC4 filters to source-native mart aliases for UBIST only."""

    if source != "ubist":
        return values

    expanded: list[str] = []
    seen: set[str] = set()
    for value in values:
        for candidate in _ubist_atc4_candidates(value):
            if candidate not in seen:
                seen.add(candidate)
                expanded.append(candidate)
    return tuple(expanded)


def _ubist_atc4_candidates(value: str) -> tuple[str, ...]:
    """Return canonical plus source-native UBIST ATC4 code candidates."""

    return atc4_source_aliases(value)


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
