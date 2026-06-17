"""Source readers for the brand-molecule bridge.

The bridge combines three molecule evidence surfaces:
1. general mart molecule dimensions, currently complete for IQVIA NSA;
2. strategic mart overlay molecules, which carry MI Master class/molecule
   overlay for UBIST strategic views;
3. strategic catalog parquet, used as a deterministic fallback for canonical
   JW brands and catalog-only molecule metadata.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path
import json

import pymysql
import pyarrow.parquet as pq

from pipeline.etl.io.mart.brand_key_normalize import normalize_brand_name
from pipeline.etl.io.mart.molecule_bridge_schema import MoleculeBridgeRecord
from pipeline.etl.io.mart.molecule_normalize import split_molecule_components


def _json_map(value: str | bytes | bytearray | None) -> Mapping[str, object]:
    """Parse a JSON object from a MariaDB JSON column."""

    if value is None:
        return {}
    text = value.decode("utf-8") if isinstance(value, (bytes, bytearray)) else value
    parsed = json.loads(text)
    return parsed if isinstance(parsed, dict) else {}


def _json_list(value: str | list[object] | None) -> tuple[str, ...]:
    """Return normalized ATC4 codes from catalog JSON/list cells."""

    if value is None:
        return ()
    raw = json.loads(value) if isinstance(value, str) and value.strip().startswith("[") else value
    if not isinstance(raw, list):
        return ()
    return tuple(str(item).strip().upper() for item in raw if str(item).strip())


def _records_from_molecule(
    *,
    brand_key: str,
    brand_name: str,
    atc4_code: str,
    mart_source: str,
    molecule_raw: str | None,
    evidence_scope: str,
) -> Iterator[MoleculeBridgeRecord]:
    """Yield one bridge record per normalized molecule component."""

    if not brand_key:
        return
    for component in split_molecule_components(molecule_raw):
        yield MoleculeBridgeRecord(
            brand_key=brand_key,
            brand_name=brand_name or brand_key,
            atc4_code=atc4_code,
            mart_source=mart_source or "any",
            molecule_norm=component.norm,
            molecule_display=component.display,
            molecule_raw=component.raw,
            evidence_scope=evidence_scope,
            component_count=component.total,
            is_combo_component=component.total > 1,
        )


def iter_general_dimension_records(
    conn: pymysql.connections.Connection,
    source_db: str,
    max_rows: int | None = None,
) -> Iterator[MoleculeBridgeRecord]:
    """Read molecule dimension buckets from ``mart_general_brand_metric``."""

    limit = f" LIMIT {int(max_rows)}" if max_rows else ""
    sql = f"""
        SELECT brand_key, brand_name, atc4_code, source, dimension_data
        FROM `{source_db}`.mart_general_brand_metric
        WHERE measure='sales' AND JSON_EXTRACT(dimension_data, '$.molecule') IS NOT NULL
        ORDER BY source, atc4_code, brand_key
        {limit}
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    for row in rows:
        dimensions = _json_map(row["dimension_data"])
        molecule_data = dimensions.get("molecule")
        if not isinstance(molecule_data, dict):
            continue
        for raw_molecule in molecule_data:
            yield from _records_from_molecule(
                brand_key=str(row["brand_key"]),
                brand_name=str(row["brand_name"]),
                atc4_code=str(row["atc4_code"] or ""),
                mart_source=str(row["source"] or "any"),
                molecule_raw=str(raw_molecule),
                evidence_scope="general_mart_dimension",
            )


def iter_strategic_overlay_records(
    conn: pymysql.connections.Connection,
    source_db: str,
    table_name: str,
    evidence_scope: str,
    max_rows: int | None = None,
) -> Iterator[MoleculeBridgeRecord]:
    """Read MI Master molecule overlay from one strategic mart brand table."""

    limit = f" LIMIT {int(max_rows)}" if max_rows else ""
    sql = f"""
        SELECT brand_key, brand_name, source, overlay_data
        FROM `{source_db}`.`{table_name}`
        WHERE measure='sales' AND JSON_EXTRACT(overlay_data, '$.molecule') IS NOT NULL
        ORDER BY source, brand_key
        {limit}
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    for row in rows:
        overlay = _json_map(row["overlay_data"])
        atc_codes = _json_list(overlay.get("allowed_atc4_codes"))
        for atc4_code in atc_codes or ("",):
            yield from _records_from_molecule(
                brand_key=str(row["brand_key"]),
                brand_name=str(row["brand_name"]),
                atc4_code=atc4_code,
                mart_source=str(row["source"] or "any"),
                molecule_raw=str(overlay.get("molecule") or ""),
                evidence_scope=evidence_scope,
            )


def iter_catalog_records(catalog_root: Path, max_rows: int | None = None) -> Iterator[MoleculeBridgeRecord]:
    """Read strategic_brand/product molecule metadata from catalog parquet."""

    brand_path = catalog_root / "strategic_brand" / "strategic_brand.parquet"
    product_path = catalog_root / "strategic_product" / "strategic_product.parquet"
    if not brand_path.exists() or not product_path.exists():
        return

    brand_rows = pq.read_table(brand_path).to_pylist()
    brand_by_id = {str(row["brand_id"]): row for row in brand_rows}
    emitted = 0
    for row in brand_rows:
        brand_key = str(row.get("general_brand_key") or normalize_brand_name(row.get("merge_name") or row.get("name")))
        atc_codes = _json_list(row.get("allowed_atc4_codes_json"))
        for atc4_code in atc_codes or ("",):
            yield from _records_from_molecule(
                brand_key=brand_key,
                brand_name=str(row.get("canonical_name") or row.get("merge_name") or row.get("name") or brand_key),
                atc4_code=atc4_code,
                mart_source="any",
                molecule_raw=str(row.get("molecule") or ""),
                evidence_scope="catalog_strategic_brand",
            )
            emitted += 1
            if max_rows and emitted >= max_rows:
                return

    for row in pq.read_table(product_path).to_pylist():
        brand = brand_by_id.get(str(row.get("brand_id")), {})
        brand_key = str(brand.get("general_brand_key") or normalize_brand_name(row.get("merge_name") or row.get("name")))
        atc_codes = _json_list(brand.get("allowed_atc4_codes_json"))
        for atc4_code in atc_codes or ("",):
            yield from _records_from_molecule(
                brand_key=brand_key,
                brand_name=str(brand.get("canonical_name") or row.get("merge_name") or row.get("name") or brand_key),
                atc4_code=atc4_code,
                mart_source="any",
                molecule_raw=str(row.get("molecule_raw") or row.get("molecule") or ""),
                evidence_scope="catalog_strategic_product",
            )
            emitted += 1
            if max_rows and emitted >= max_rows:
                return
