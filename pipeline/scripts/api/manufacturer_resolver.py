"""Canonical IQVIA manufacturer identity resolver for every serving path.

This module is the single source of truth for product-to-manufacturer selection.
Consumers must import its public API instead of copying the query, normalization,
selection, or ordering rules.

The in-memory map is process-local and lives for 24 hours. It has no table-change
trigger: MI Master or ``iqvia_nsa_quarterly_raw`` updates become visible after the
next TTL refresh or pod restart. Pods can therefore temporarily observe different
source snapshots for at most one TTL. ``MANUFACTURER_RESOLVER_REVISION`` identifies
the resolver algorithm and contract, not a source-data snapshot; consumers should
include it in diagnostic logs when comparing paths. It is intentionally not added
to public response payloads because this extraction must remain byte-identical.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from threading import Lock
from typing import Final, TypeAlias

from pipeline.scripts.analysis.brand_activity.alias.normalize import normalize_iqvia_en
from pipeline.scripts.api import db
from pipeline.scripts.api.config import config
from pipeline.scripts.api.dynamic_market.types import quote_identifier


ManufacturerMap: TypeAlias = Mapping[str, frozenset[str]]

MANUFACTURER_RESOLVER_REVISION: Final = "iqvia-mfr-kor-v1"
MANUFACTURER_CACHE_TTL_SECONDS: Final = 86400.0

_manufacturer_cache: tuple[float, dict[str, frozenset[str]]] | None = None
_manufacturer_cache_lock = Lock()


def fetch_manufacturer_by_product() -> dict[str, frozenset[str]]:
    """Load normalized IQVIA product names and their Korean manufacturers.

    Returns every non-empty ``PRODUCT NAME`` / ``MFR NAME KOR`` pair in the
    serving database as an immutable manufacturer set keyed by
    ``normalize_iqvia_en(PRODUCT NAME)``. Duplicate source rows collapse through
    SQL ``DISTINCT`` and set membership. Database and configuration exceptions
    propagate unchanged to the caller; an unavailable source must not silently
    become an empty mapping.
    """

    schema = quote_identifier(config.db_name)
    rows = db.fetch_all(
        f"""
        SELECT DISTINCT
            JSON_UNQUOTE(JSON_EXTRACT(payload, '$.static."PRODUCT NAME"')) AS product,
            JSON_UNQUOTE(JSON_EXTRACT(payload, '$.static."MFR NAME KOR"')) AS manufacturer
        FROM {schema}.`iqvia_nsa_quarterly_raw`
        """
    )
    mapping: dict[str, set[str]] = {}
    for row in rows:
        raw_product = row.get("product")
        raw_manufacturer = row.get("manufacturer")
        product = normalize_iqvia_en(raw_product if isinstance(raw_product, str) else "")
        manufacturer = raw_manufacturer.strip() if isinstance(raw_manufacturer, str) else ""
        if not product or not manufacturer:
            continue
        mapping.setdefault(product, set()).add(manufacturer)
    return {product: frozenset(names) for product, names in mapping.items()}


def get_manufacturer_by_product() -> dict[str, frozenset[str]]:
    """Return the process-local manufacturer map, refreshing after the 24h TTL.

    The first call loads the serving database. Later calls in the same process
    reuse that mapping until ``MANUFACTURER_CACHE_TTL_SECONDS`` elapses. There is
    no external invalidation signal; a pod restart or TTL expiry is the only
    refresh mechanism. Load exceptions propagate and do not populate the cache.
    """

    global _manufacturer_cache

    now = time.monotonic()
    cached = _manufacturer_cache
    if cached is not None and now - cached[0] < MANUFACTURER_CACHE_TTL_SECONDS:
        return cached[1]
    with _manufacturer_cache_lock:
        cached = _manufacturer_cache
        if cached is not None and now - cached[0] < MANUFACTURER_CACHE_TTL_SECONDS:
            return cached[1]
        mapping = fetch_manufacturer_by_product()
        _manufacturer_cache = (time.monotonic(), mapping)
        return mapping


def resolve_manufacturer_name(
    product_codes: Sequence[str],
    manufacturer_map: ManufacturerMap | None = None,
) -> str | None:
    """Resolve product codes to one deterministic Korean manufacturer label.

    All supplied product rows participate; this function does not select a period,
    latest row, or representative product. Each product is normalized with
    ``normalize_iqvia_en`` and contributes one hit to every manufacturer attached
    to it. Zero candidates return ``None`` (never an empty string or placeholder),
    one candidate returns that name, and multiple candidates are all retained as
    a comma-joined label ordered by hit count descending and then name ascending.
    Keeping every display candidate is intentional and differs from brand-alias
    resolution, where ambiguity must be rejected to avoid selecting wrong data.

    When ``manufacturer_map`` is omitted, the process-local cached map is used.
    Mapping and normalization exceptions propagate unchanged.
    """

    mapping = manufacturer_map if manufacturer_map is not None else get_manufacturer_by_product()
    counts: dict[str, int] = {}
    for code in product_codes:
        for manufacturer in mapping.get(normalize_iqvia_en(code), frozenset()):
            counts[manufacturer] = counts.get(manufacturer, 0) + 1
    if not counts:
        return None
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return ", ".join(name for name, _count in ordered)
