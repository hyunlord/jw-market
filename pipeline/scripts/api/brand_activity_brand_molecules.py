from __future__ import annotations

from functools import lru_cache
from typing import Final, Mapping

from pipeline.domain.molecules import split_molecule_components
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
    """Return latest-quarter raw NSA molecule bridge rows for candidate product codes."""
    # F-055: the previous per-chunk query re-evaluated JSON_EXTRACT over every
    # latest-quarter row (~45k longtext payloads, ~2s CPU) once per market.
    # Load the distinct (product, molecule) pairs once per process instead and
    # filter in Python with the same exact-match semantics as the old IN list.
    wanted = set(product_codes)
    return [
        {"product_code": product_code, "molecule": molecule}
        for product_code, molecule in _latest_quarter_molecule_pairs()
        if product_code in wanted
    ]


@lru_cache(maxsize=1)
def _latest_quarter_molecule_pairs() -> tuple[tuple[str | None, str | None], ...]:
    """Read all distinct latest-quarter (product, molecule) payload pairs once."""
    mart_db = quote_identifier(config.db_name)
    rows = db.fetch_all(
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
        """,
        (GENERAL_RAW_PRODUCT_PATH, GENERAL_RAW_MOLECULE_PATH),
    )
    return tuple((row.get("product_code"), row.get("molecule")) for row in rows)


def _chunks(values: tuple[str, ...], *, size: int) -> tuple[tuple[str, ...], ...]:
    """Split product codes into SQL placeholder batches."""
    return tuple(values[index : index + size] for index in range(0, len(values), size))
