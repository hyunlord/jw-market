"""Resolve the explicit deep-analysis market context without silent tie-breaks."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Final, Literal, TypeAlias, cast

from pipeline.scripts.api import db
from pipeline.scripts.utils.brand_name_normalize import compact_brand_name


DeepAnalysisViewKind: TypeAlias = Literal["general", "strategic_ml", "strategic_cd"]
DeepAnalysisSource: TypeAlias = Literal["ubist", "iqvia"]

VIEW_KINDS: Final[tuple[DeepAnalysisViewKind, ...]] = ("general", "strategic_ml", "strategic_cd")
SOURCES: Final[tuple[DeepAnalysisSource, ...]] = ("ubist", "iqvia")
SOURCE_TO_DB: Final[dict[DeepAnalysisSource, str]] = {"ubist": "ubist", "iqvia": "iqvia_nsa"}
DB_TO_SOURCE: Final[dict[str, DeepAnalysisSource]] = {value: key for key, value in SOURCE_TO_DB.items()}


@dataclass(frozen=True, slots=True)
class DeepAnalysisContext:
    brand_key: str
    brand_name: str
    view_kind: DeepAnalysisViewKind
    market_id: str
    market_name: str | None
    source: DeepAnalysisSource
    db_source: str
    in_catalog: bool
    has_market_data: bool
    market_allowed_sources: tuple[DeepAnalysisSource, ...]
    brand_available_sources: tuple[str, ...]

    def public(self) -> dict[str, Any]:
        return {
            "view_kind": self.view_kind,
            "market_id": self.market_id,
            "market_name": self.market_name,
            "source": self.source,
            "has_market_data": self.has_market_data,
        }


@dataclass(frozen=True, slots=True)
class DeepAnalysisContextError(Exception):
    status_code: int
    error: str
    message: str
    available_contexts: tuple[dict[str, Any], ...] = ()

    def detail(self) -> dict[str, Any]:
        return {
            "error": self.error,
            "message": self.message,
            "available_contexts": list(self.available_contexts),
        }

    def __str__(self) -> str:
        return self.message


def resolve_deep_analysis_context(
    *,
    brand: str,
    view_kind: str,
    market_id: str | None,
    source: str | None,
) -> DeepAnalysisContext:
    """Resolve exactly one serving context or return all valid alternatives."""

    normalized_view = _normalize_view_kind(view_kind)
    normalized_source = _normalize_source(source) if source is not None else None
    normalized_market = _normalize_market_id(normalized_view, market_id) if market_id else None
    candidates = (
        _general_contexts(brand)
        if normalized_view == "general"
        else _strategic_contexts(brand, normalized_view)
    )
    available = _public_contexts(candidates)
    if not candidates:
        raise DeepAnalysisContextError(
            status_code=404,
            error="brand_not_found",
            message="brand has no serving context for the requested view",
        )

    market_candidates = candidates
    if normalized_market is not None:
        market_candidates = tuple(item for item in candidates if item.market_id == normalized_market)
        if not market_candidates:
            raise DeepAnalysisContextError(
                status_code=404,
                error="market_membership_not_found",
                message="brand is not a member of the requested market",
                available_contexts=available,
            )

    source_candidates = market_candidates
    if normalized_source is not None:
        source_candidates = tuple(item for item in market_candidates if item.source == normalized_source)
        if not source_candidates:
            raise DeepAnalysisContextError(
                status_code=422,
                error="source_not_available",
                message="source is not available for the requested market",
                available_contexts=_public_contexts(market_candidates),
            )

    if len(source_candidates) != 1:
        market_count = len({item.market_id for item in source_candidates})
        error = "ambiguous_market_context" if market_count > 1 else "ambiguous_source_context"
        message = (
            "market_id is required because the brand belongs to multiple markets"
            if market_count > 1
            else "source is required because the market supports multiple sources"
        )
        raise DeepAnalysisContextError(
            status_code=409,
            error=error,
            message=message,
            available_contexts=_public_contexts(source_candidates),
        )
    return source_candidates[0]


def _normalize_view_kind(value: str) -> DeepAnalysisViewKind:
    normalized = value.strip().lower()
    if normalized not in VIEW_KINDS:
        raise DeepAnalysisContextError(
            status_code=422,
            error="invalid_view_kind",
            message=f"unsupported view_kind: {value}",
        )
    return cast(DeepAnalysisViewKind, normalized)


def _normalize_source(value: str) -> DeepAnalysisSource:
    normalized = value.strip().lower()
    if normalized not in SOURCES:
        raise DeepAnalysisContextError(
            status_code=422,
            error="invalid_source",
            message=f"unsupported source: {value}",
        )
    return cast(DeepAnalysisSource, normalized)


def _normalize_market_id(view_kind: DeepAnalysisViewKind, value: str) -> str:
    normalized = value.strip()
    if view_kind == "general":
        normalized = normalized.upper()
        if not normalized:
            raise _invalid_market_id(value, view_kind)
        return normalized
    if view_kind == "strategic_ml":
        normalized = normalized.lower()
        if normalized.startswith("strategy_"):
            suffix = normalized.removeprefix("strategy_")
            if suffix.isdigit():
                normalized = f"ml_{int(suffix):03d}"
        if re.fullmatch(r"ml_\d+", normalized) is None:
            raise _invalid_market_id(value, view_kind)
        return normalized
    normalized = normalized.lower()
    if re.fullmatch(r"cd_\d+", normalized) is None:
        raise _invalid_market_id(value, view_kind)
    return normalized


def _invalid_market_id(value: str, view_kind: str) -> DeepAnalysisContextError:
    return DeepAnalysisContextError(
        status_code=422,
        error="invalid_market_id",
        message=f"market_id {value!r} does not match view_kind {view_kind!r}",
    )


def _general_contexts(brand: str) -> tuple[DeepAnalysisContext, ...]:
    rows = _general_rows(brand)
    if not rows:
        return ()
    identities = {
        str(row.get("brand_key") or row.get("brand_name") or "").strip()
        for row in rows
        if row.get("brand_key") or row.get("brand_name")
    }
    if len(identities) > 1:
        raise DeepAnalysisContextError(
            status_code=409,
            error="ambiguous_brand",
            message="compact brand lookup matched multiple general brands",
        )
    brand_available_sources = tuple(sorted({str(row.get("source") or "") for row in rows if row.get("source")}))
    by_market: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        market = str(row.get("atc4_code") or "").strip().upper()
        if market:
            by_market.setdefault(market, []).append(row)

    contexts: list[DeepAnalysisContext] = []
    for market, market_rows in sorted(by_market.items()):
        allowed = tuple(
            sorted(
                {
                    DB_TO_SOURCE[db_source]
                    for row in market_rows
                    if (db_source := str(row.get("source") or "")) in DB_TO_SOURCE
                }
            )
        )
        base = market_rows[0]
        for api_source in allowed:
            contexts.append(
                DeepAnalysisContext(
                    brand_key=str(base.get("brand_key") or brand),
                    brand_name=str(base.get("brand_name") or brand),
                    view_kind="general",
                    market_id=market,
                    market_name=_optional_text(base.get("market_name")),
                    source=api_source,
                    db_source=SOURCE_TO_DB[api_source],
                    in_catalog=True,
                    has_market_data=True,
                    market_allowed_sources=allowed,
                    brand_available_sources=brand_available_sources,
                )
            )
    return tuple(contexts)


def _general_rows(brand: str) -> list[dict[str, Any]]:
    rows = db.fetch_all(
        """
        SELECT DISTINCT brand_key, brand_name, atc4_code, atc4_desc AS market_name, source
        FROM mart_general_brand_metric
        WHERE brand_key = %s OR brand_name = %s
        ORDER BY atc4_code, source
        """,
        (brand, brand),
    )
    if rows:
        return rows
    compact = compact_brand_name(brand)
    if not compact:
        return []
    return db.fetch_all(
        f"""
        SELECT DISTINCT brand_key, brand_name, atc4_code, atc4_desc AS market_name, source
        FROM mart_general_brand_metric
        WHERE {_compact_sql('brand_key')} = %s OR {_compact_sql('brand_name')} = %s
        ORDER BY brand_name, atc4_code, source
        """,
        (compact, compact),
    )


def _strategic_contexts(
    brand: str,
    view_kind: Literal["strategic_ml", "strategic_cd"],
) -> tuple[DeepAnalysisContext, ...]:
    catalog_rows = _strategic_catalog_rows(brand, view_kind)
    if not catalog_rows:
        return ()
    identities = {
        (str(row.get("brand_key") or ""), str(row.get("brand_name") or ""))
        for row in catalog_rows
    }
    if len(identities) > 1:
        raise DeepAnalysisContextError(
            status_code=409,
            error="ambiguous_brand",
            message="compact brand lookup matched multiple catalog brands",
        )

    base = catalog_rows[0]
    matched_brand_key = str(base.get("brand_key") or brand)
    matched_brand_name = str(base.get("brand_name") or brand)
    mart_rows = _strategic_mart_rows(
        requested_brand=brand,
        brand_key=matched_brand_key,
        brand_name=matched_brand_name,
        view_kind=view_kind,
    )
    data_pairs = {
        (str(row.get("market_id") or ""), str(row.get("source") or ""))
        for row in mart_rows
    }
    brand_available_sources = _brand_available_sources(brand, matched_brand_key, matched_brand_name)
    contexts: list[DeepAnalysisContext] = []
    for row in catalog_rows:
        market = str(row.get("market_id") or "")
        allowed = _catalog_sources(row.get("data_source"))
        for api_source in allowed:
            db_source = SOURCE_TO_DB[api_source]
            contexts.append(
                DeepAnalysisContext(
                    brand_key=matched_brand_key,
                    brand_name=matched_brand_name,
                    view_kind=view_kind,
                    market_id=market,
                    market_name=_optional_text(row.get("market_name")),
                    source=api_source,
                    db_source=db_source,
                    in_catalog=True,
                    has_market_data=(market, db_source) in data_pairs,
                    market_allowed_sources=allowed,
                    brand_available_sources=brand_available_sources,
                )
            )
    return _deduplicate_contexts(contexts)


def _strategic_catalog_rows(
    brand: str,
    view_kind: Literal["strategic_ml", "strategic_cd"],
) -> list[dict[str, Any]]:
    market_table = "catalog_ml_market" if view_kind == "strategic_ml" else "catalog_cd_market"
    brand_market_column = "ml_id" if view_kind == "strategic_ml" else "cd_id"
    market_id_column = brand_market_column
    exact = db.fetch_all(
        f"""
        SELECT COALESCE(NULLIF(b.general_brand_key, ''), NULLIF(b.canonical_name, ''), b.name) AS brand_key,
               b.name AS brand_name,
               b.{brand_market_column} AS market_id,
               m.name AS market_name,
               m.data_source
        FROM catalog_strategic_brand b
        JOIN {market_table} m ON m.{market_id_column} = b.{brand_market_column}
        WHERE COALESCE(b.is_excluded, 0) = 0
          AND b.{brand_market_column} IS NOT NULL
          AND (b.name = %s OR b.canonical_name = %s OR b.general_brand_key = %s)
        ORDER BY b.{brand_market_column}, b.name
        """,
        (brand, brand, brand),
    )
    if exact:
        return exact
    compact = compact_brand_name(brand)
    if not compact:
        return []
    return db.fetch_all(
        f"""
        SELECT COALESCE(NULLIF(b.general_brand_key, ''), NULLIF(b.canonical_name, ''), b.name) AS brand_key,
               b.name AS brand_name,
               b.{brand_market_column} AS market_id,
               m.name AS market_name,
               m.data_source
        FROM catalog_strategic_brand b
        JOIN {market_table} m ON m.{market_id_column} = b.{brand_market_column}
        WHERE COALESCE(b.is_excluded, 0) = 0
          AND b.{brand_market_column} IS NOT NULL
          AND ({_compact_sql('b.name')} = %s
               OR {_compact_sql('b.canonical_name')} = %s
               OR {_compact_sql('b.general_brand_key')} = %s)
        ORDER BY b.name, b.{brand_market_column}
        """,
        (compact, compact, compact),
    )


def _strategic_mart_rows(
    *,
    requested_brand: str,
    brand_key: str,
    brand_name: str,
    view_kind: Literal["strategic_ml", "strategic_cd"],
) -> list[dict[str, Any]]:
    table = "mart_strategic_ml_brand_metric" if view_kind == "strategic_ml" else "mart_strategic_cd_brand_metric"
    id_column = "ml_id" if view_kind == "strategic_ml" else "cd_market_id"
    return db.fetch_all(
        f"""
        SELECT DISTINCT {id_column} AS market_id, source
        FROM {table}
        WHERE brand_key IN (%s, %s, %s) OR brand_name IN (%s, %s, %s)
        ORDER BY {id_column}, source
        """,
        (requested_brand, brand_key, brand_name, requested_brand, brand_key, brand_name),
    )


def _brand_available_sources(brand: str, brand_key: str, brand_name: str) -> tuple[str, ...]:
    rows = db.fetch_all(
        """
        SELECT DISTINCT source
        FROM mart_general_brand_metric
        WHERE brand_key IN (%s, %s, %s) OR brand_name IN (%s, %s, %s)
        ORDER BY source
        """,
        (brand, brand_key, brand_name, brand, brand_key, brand_name),
    )
    return tuple(str(row["source"]) for row in rows if row.get("source"))


def _catalog_sources(value: object) -> tuple[DeepAnalysisSource, ...]:
    text = str(value or "").upper()
    sources: list[DeepAnalysisSource] = []
    if "UBIST" in text:
        sources.append("ubist")
    if "IQVIA" in text:
        sources.append("iqvia")
    return tuple(sources)


def _deduplicate_contexts(contexts: list[DeepAnalysisContext]) -> tuple[DeepAnalysisContext, ...]:
    unique: dict[tuple[str, str], DeepAnalysisContext] = {}
    for context in contexts:
        unique[(context.market_id, context.source)] = context
    return tuple(unique[key] for key in sorted(unique))


def _public_contexts(contexts: tuple[DeepAnalysisContext, ...]) -> tuple[dict[str, Any], ...]:
    return tuple(item.public() for item in contexts)


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _compact_sql(column: str) -> str:
    return f"REPLACE(REPLACE(REPLACE(REPLACE({column}, ' ', ''), CHAR(9), ''), CHAR(10), ''), CHAR(13), '')"
