"""Lightweight CSD source-presence lookup for Brand Activity."""

from __future__ import annotations

import time
from dataclasses import dataclass
from threading import Lock
from typing import Final, Mapping

from typing_extensions import TypedDict

from pipeline.scripts.analysis.brand_activity.alias.normalize import normalize_iqvia_en
from pipeline.scripts.api import db
from pipeline.scripts.api.brand_activity_brand_resolver import _product_codes
from pipeline.scripts.api.brand_activity_csd_shared import SOURCE
from pipeline.scripts.api.config import config
from pipeline.scripts.api.dynamic_market.types import quote_identifier
from pipeline.scripts.utils.brand_name_normalize import compact_brand_name


CSD_PRODUCT_CACHE_TTL_SECONDS: Final = 60.0


class CsdPresence(TypedDict):
    brand: str
    resolved: bool
    csd_present: bool
    csd_source: bool
    keyword_source: bool
    reason: str | None


@dataclass(frozen=True, slots=True)
class _BrandRowIndex:
    by_key: dict[str, list[dict[str, object]]]
    by_name: dict[str, list[dict[str, object]]]
    by_compact_key: dict[str, list[dict[str, object]]]
    by_compact_name: dict[str, list[dict[str, object]]]


_csd_product_cache: tuple[float, frozenset[str]] | None = None
_csd_product_cache_lock = Lock()
_keyword_product_cache: tuple[float, frozenset[str]] | None = None
_keyword_product_cache_lock = Lock()


def get_csd_presence(brand: str) -> CsdPresence:
    """Return whether one resolved brand has activity in the CSD or keyword source."""

    return get_csd_presences((brand,))[0]


def get_csd_presences(brands: tuple[str, ...]) -> list[CsdPresence]:
    """Resolve one request batch with one mart read plus one cached read per source axis.

    ``csd_present`` is the union gate (CSD ∪ keyword): the front end enables the whole
    Brand Activity tab on it. ``csd_source``/``keyword_source`` expose each axis so a
    chart with an empty axis renders empty state instead of blocking the tab. Both source
    sets share the same cache lifetime so a stale half cannot flip the gate.
    """

    rows = _index_brand_rows(_fetch_brand_rows(brands))
    csd_products = _cached_csd_products()
    keyword_products = _cached_keyword_products()
    return [
        _presence_for_rows(brand, _rows_for_brand(rows, brand), csd_products, keyword_products)
        for brand in brands
    ]


def iqvia_product_codes_by_brand(brands: Mapping[str, str]) -> dict[str, tuple[str, ...]]:
    """Return IQVIA product codes for mart brand keys, independent of request source."""

    aliases = tuple(dict.fromkeys((*brands.keys(), *brands.values())))
    rows = _index_brand_rows(_fetch_brand_rows(aliases))
    result: dict[str, tuple[str, ...]] = {}
    for brand_key, brand_name in brands.items():
        matched_rows = _rows_for_brand(rows, brand_key) or _rows_for_brand(rows, brand_name)
        product_codes = {
            normalize_iqvia_en(code)
            for row in matched_rows
            for code in _product_codes(row.get("by_dimension"))
        }
        result[brand_key] = tuple(sorted(product_codes))
    return result


def _fetch_brand_rows(brands: tuple[str, ...]) -> list[dict[str, object]]:
    if not brands:
        return []
    table = f"{quote_identifier(config.db_name)}.{quote_identifier('mart_general_brand_metric')}"
    placeholders = ", ".join(["%s"] * len(brands))
    exact = db.fetch_all(
        f"""
        SELECT DISTINCT brand_key, brand_name, by_dimension
        FROM {table}
        WHERE source = %s AND measure = 'sales'
          AND (brand_key IN ({placeholders})
               OR brand_name IN ({placeholders}))
        ORDER BY brand_key
        """,
        (SOURCE, *brands, *brands),
    )
    unresolved = tuple(
        brand
        for brand in brands
        if not any(
            str(row.get("brand_key") or "") == brand or str(row.get("brand_name") or "") == brand
            for row in exact
        )
    )
    if not unresolved:
        return exact
    compact_brands = tuple(compact_brand_name(brand) for brand in unresolved)
    compact_placeholders = ", ".join(["%s"] * len(compact_brands))
    fallback = db.fetch_all(
        f"""
        SELECT DISTINCT brand_key, brand_name, by_dimension
        FROM {table}
        WHERE source = %s AND measure = 'sales'
          AND (REPLACE(brand_key, ' ', '') IN ({compact_placeholders})
               OR REPLACE(brand_name, ' ', '') IN ({compact_placeholders}))
        ORDER BY brand_key
        """,
        (SOURCE, *compact_brands, *compact_brands),
    )
    return [*exact, *fallback]


def _fetch_csd_products() -> frozenset[str]:
    rows = db.fetch_all(
        f"""
        SELECT DISTINCT master_product
        FROM {quote_identifier(config.brand_activity_db_name)}.`csd_channel_dynamics_stage`
        WHERE jw_channel = 'TOTAL'
        """
    )
    return frozenset(normalize_iqvia_en(str(row["master_product"])) for row in rows)


def _cached_csd_products() -> frozenset[str]:
    global _csd_product_cache

    now = time.monotonic()
    cached = _csd_product_cache
    if cached is not None and now - cached[0] < CSD_PRODUCT_CACHE_TTL_SECONDS:
        return cached[1]
    with _csd_product_cache_lock:
        cached = _csd_product_cache
        if cached is not None and now - cached[0] < CSD_PRODUCT_CACHE_TTL_SECONDS:
            return cached[1]
        products = _fetch_csd_products()
        _csd_product_cache = (time.monotonic(), products)
        return products


def _fetch_keyword_products() -> frozenset[str]:
    rows = db.fetch_all(
        f"""
        SELECT DISTINCT product_name
        FROM {quote_identifier(config.brand_activity_db_name)}.`km_keyword_event_stage`
        """
    )
    return frozenset(
        normalize_iqvia_en(str(row["product_name"]))
        for row in rows
        if row.get("product_name")
    )


def _cached_keyword_products() -> frozenset[str]:
    global _keyword_product_cache

    now = time.monotonic()
    cached = _keyword_product_cache
    if cached is not None and now - cached[0] < CSD_PRODUCT_CACHE_TTL_SECONDS:
        return cached[1]
    with _keyword_product_cache_lock:
        cached = _keyword_product_cache
        if cached is not None and now - cached[0] < CSD_PRODUCT_CACHE_TTL_SECONDS:
            return cached[1]
        products = _fetch_keyword_products()
        _keyword_product_cache = (time.monotonic(), products)
        return products


def _index_brand_rows(rows: list[dict[str, object]]) -> _BrandRowIndex:
    index = _BrandRowIndex({}, {}, {}, {})
    for row in rows:
        brand_key = str(row.get("brand_key") or "")
        brand_name = str(row.get("brand_name") or "")
        index.by_key.setdefault(brand_key, []).append(row)
        index.by_name.setdefault(brand_name, []).append(row)
        index.by_compact_key.setdefault(compact_brand_name(brand_key), []).append(row)
        index.by_compact_name.setdefault(compact_brand_name(brand_name), []).append(row)
    return index


def _rows_for_brand(rows: _BrandRowIndex, brand: str) -> list[dict[str, object]]:
    exact_key = rows.by_key.get(brand, [])
    if exact_key:
        return _unique_identity(exact_key)
    exact_name = rows.by_name.get(brand, [])
    if exact_name:
        return _unique_identity(exact_name)
    compact = compact_brand_name(brand)
    compact_rows = [
        *rows.by_compact_key.get(compact, []),
        *rows.by_compact_name.get(compact, []),
    ]
    return _unique_identity(compact_rows)


def _unique_identity(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    identities = {str(row.get("brand_key") or row.get("brand_name") or "") for row in rows}
    return rows if len(identities) == 1 else []


def _presence_for_rows(
    brand: str,
    rows: list[dict[str, object]],
    csd_products: frozenset[str],
    keyword_products: frozenset[str],
) -> CsdPresence:
    if not rows:
        return _result(
            brand,
            resolved=False,
            csd_present=False,
            csd_source=False,
            keyword_source=False,
            reason="brand_not_found",
        )
    product_codes = {
        normalize_iqvia_en(code)
        for row in rows
        for code in _product_codes(row.get("by_dimension"))
    }
    csd_source = bool(product_codes & csd_products)
    keyword_source = bool(product_codes & keyword_products)
    csd_present = csd_source or keyword_source
    # Gate is the union; reason is null when either axis has data. "no_csd_mapping" is
    # removed because it ignored the keyword axis and read like a mapping defect.
    reason = None if csd_present else "no_activity_any_source"
    return _result(
        brand,
        resolved=True,
        csd_present=csd_present,
        csd_source=csd_source,
        keyword_source=keyword_source,
        reason=reason,
    )


def _result(
    brand: str,
    *,
    resolved: bool,
    csd_present: bool,
    csd_source: bool,
    keyword_source: bool,
    reason: str | None,
) -> CsdPresence:
    return {
        "brand": brand,
        "resolved": resolved,
        "csd_present": csd_present,
        "csd_source": csd_source,
        "keyword_source": keyword_source,
        "reason": reason,
    }
