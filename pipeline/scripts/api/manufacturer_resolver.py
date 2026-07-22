"""Dependency-light IQVIA manufacturer identity resolver for serving paths."""

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
    """Load normalized IQVIA product names and their Korean manufacturers."""

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
    """Return the serving-DB manufacturer map, cached once per long-lived pod."""

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
    """Resolve all product rows to one deterministic Korean manufacturer label.

    Each normalized product contributes one hit to every manufacturer attached to that
    product. Manufacturers sort by hit count descending, then name ascending, and all names
    are retained as a comma-joined label. An unmapped product set returns ``None``.
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
