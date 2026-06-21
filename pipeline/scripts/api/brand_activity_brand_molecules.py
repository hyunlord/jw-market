from __future__ import annotations

from functools import lru_cache
from typing import Final, Mapping

from pipeline.etl.io.mart.molecule_normalize import split_molecule_components
from pipeline.scripts.analysis.brand_activity.alias.normalize import normalize_iqvia_en
from pipeline.scripts.api import db
from pipeline.scripts.api.brand_activity_csd_shared import BrandMeta, JsonMap, text
from pipeline.scripts.api.config import config
from pipeline.scripts.api.dynamic_market.types import quote_identifier


GENERAL_RAW_PRODUCT_PATH: Final = '$."static"."PRODUCT NAME"'
GENERAL_RAW_MOLECULE_PATH: Final = '$."static"."MOLECULE DESC"'
RAW_MOLECULE_CHUNK_SIZE: Final = 500


def general_molecules_by_product(metas: Mapping[str, BrandMeta]) -> dict[str, tuple[str, ...]]:
    """Return normalized NSA molecule names keyed by normalized IQVIA product code."""
    product_codes = tuple(sorted({code for meta in metas.values() for code in meta.product_codes}))
    if not product_codes:
        return {}
    result: dict[str, set[str]] = {code: set() for code in product_codes}
    for chunk in _chunks(product_codes, size=RAW_MOLECULE_CHUNK_SIZE):
        rows = _fetch_raw_molecules(chunk)
        for row in rows:
            product_code = normalize_iqvia_en(str(row.get("product_code") or ""))
            for component in split_molecule_components(text(row.get("molecule"))):
                result.setdefault(product_code, set()).add(component.norm)
    return {code: tuple(sorted(values)) for code, values in result.items()}


@lru_cache(maxsize=64)
def _fetch_raw_molecules(product_codes: tuple[str, ...]) -> list[JsonMap]:
    """Read latest-quarter raw NSA molecule bridge rows for candidate product codes."""
    placeholders = ", ".join(["%s"] * len(product_codes))
    mart_db = quote_identifier(config.db_name)
    return db.fetch_all(
        f"""
        SELECT DISTINCT
          JSON_UNQUOTE(JSON_EXTRACT(payload, %s)) AS product_code,
          JSON_UNQUOTE(JSON_EXTRACT(payload, %s)) AS molecule
        FROM {mart_db}.`iqvia_nsa_quarterly_raw`
        WHERE period_yyyy = (SELECT MAX(period_yyyy) FROM {mart_db}.`iqvia_nsa_quarterly_raw`)
          AND period_quarter = (
            SELECT MAX(period_quarter)
            FROM {mart_db}.`iqvia_nsa_quarterly_raw`
            WHERE period_yyyy = (SELECT MAX(period_yyyy) FROM {mart_db}.`iqvia_nsa_quarterly_raw`)
          )
          AND JSON_UNQUOTE(JSON_EXTRACT(payload, %s)) IN ({placeholders})
        """,
        (GENERAL_RAW_PRODUCT_PATH, GENERAL_RAW_MOLECULE_PATH, GENERAL_RAW_PRODUCT_PATH, *product_codes),
    )


def _chunks(values: tuple[str, ...], *, size: int) -> tuple[tuple[str, ...], ...]:
    """Split product codes into SQL placeholder batches."""
    return tuple(values[index : index + size] for index in range(0, len(values), size))
