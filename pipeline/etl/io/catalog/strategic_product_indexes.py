from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from pipeline.etl.io.catalog.raw_sources import read_parquet_compat_rows
from pipeline.etl.io.catalog.strategic_product_schema import IQVIA_JOIN_KEY_BY_SMID, UBIST_JOIN_KEY_BY_SMID
from pipeline.etl.io.catalog.strategic_product_text import clean_text, manufacturer_key, normalize_key

def unique_candidates(rows: list[dict[str, Any]], identity_columns: tuple[str, ...]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        key = tuple(row.get(column) for column in identity_columns)
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out

def load_ubist_indexes(path: Path) -> dict[str, dict[tuple[str, str], list[dict[str, Any]]]]:
    columns = [
        "product_key",
        "product_name",
        "brand",
        "manufacturer",
        "molecule_strength",
        "molecule",
        "formulation",
        "insurance_type",
    ]
    rows = read_parquet_compat_rows(
        path,
        columns,
        {
            "product_key": "약품코드",
            "product_name": "제품",
            "brand": "브랜드",
            "manufacturer": "제조사",
            "molecule_strength": "성분용량",
            "molecule": "성분",
            "formulation": "제형",
            "insurance_type": "급여구분",
        },
    )
    brand_index: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    product_index: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        candidate = {
            "source_view": "UBIST",
            "source_product_key": clean_text(row.get("product_key")),
            "product_name": clean_text(row.get("product_name")),
            "brand": clean_text(row.get("brand")),
            "manufacturer": clean_text(row.get("manufacturer")),
            "molecule": clean_text(row.get("molecule")),
            "strength_pack": clean_text(row.get("molecule_strength")),
            "dosage_form": clean_text(row.get("formulation")),
            "nhi_type": clean_text(row.get("insurance_type")),
        }
        mfr = manufacturer_key(candidate["manufacturer"])
        brand_key = normalize_key(candidate["brand"])
        product_key = normalize_key(candidate["product_name"])
        if brand_key and mfr:
            brand_index[(brand_key, mfr)].append(candidate)
        if product_key and mfr:
            product_index[(product_key, mfr)].append(candidate)

    identity = ("product_name", "manufacturer", "strength_pack", "dosage_form", "nhi_type")
    return {
        "ubist_brand_manufacturer": {
            key: unique_candidates(value, identity) for key, value in brand_index.items()
        },
        "ubist_product_manufacturer": {
            key: unique_candidates(value, identity) for key, value in product_index.items()
        },
    }


def load_iqvia_indexes(path: Path) -> dict[str, dict[tuple[str, ...], list[dict[str, Any]]]]:
    columns = [
        "product_key",
        "product_name",
        "pack_desc",
        "mfr_name_kor",
        "atc4_code",
        "strength",
        "molecule_desc",
        "nhi_type",
        "nfc3_desc",
    ]
    table = pq.read_table(path, columns=columns)
    pack_index: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    mfr_atc_mol_index: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    atc_mol_index: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in table.to_pylist():
        candidate = {
            "source_view": "IQVIA",
            "source_product_key": clean_text(row.get("product_key")),
            "product_name": clean_text(row.get("product_name")),
            "pack_desc": clean_text(row.get("pack_desc")),
            "manufacturer": clean_text(row.get("mfr_name_kor")),
            "atc4_code": clean_text(row.get("atc4_code")),
            "molecule": clean_text(row.get("molecule_desc")),
            "strength_pack": clean_text(row.get("pack_desc")) or clean_text(row.get("strength")),
            "dosage_form": clean_text(row.get("nfc3_desc")),
            "nhi_type": clean_text(row.get("nhi_type")),
        }
        mfr = manufacturer_key(candidate["manufacturer"])
        atc = normalize_key(candidate["atc4_code"])
        molecule = normalize_key(candidate["molecule"])
        pack = normalize_key(candidate["pack_desc"])
        if pack and mfr and atc:
            pack_index[(pack, mfr, atc)].append(candidate)
        if mfr and atc and molecule:
            mfr_atc_mol_index[(mfr, atc, molecule)].append(candidate)
        if atc and molecule:
            atc_mol_index[(atc, molecule)].append(candidate)

    identity = ("product_name", "pack_desc", "manufacturer", "atc4_code", "molecule", "nhi_type")
    return {
        "iqvia_pack_manufacturer_atc4": {
            key: unique_candidates(value, identity) for key, value in pack_index.items()
        },
        "iqvia_manufacturer_atc4_molecule": {
            key: unique_candidates(value, identity) for key, value in mfr_atc_mol_index.items()
        },
        "iqvia_atc4_molecule": {
            key: unique_candidates(value, identity) for key, value in atc_mol_index.items()
        },
    }


def ubist_candidates(
    brand_row: dict[str, Any],
    context: dict[str, Any],
    indexes: dict[str, dict[tuple[str, str], list[dict[str, Any]]]],
) -> tuple[str | None, list[dict[str, Any]]]:
    smid = str(context["strategic_market_id"])
    join_key = UBIST_JOIN_KEY_BY_SMID.get(smid)
    if join_key is None:
        return None, []
    name_key = normalize_key(context.get("product_name") or brand_row.get("name"))
    mfr_key = manufacturer_key(context.get("manufacturer") or brand_row.get("제조사"))
    if not name_key or not mfr_key:
        return join_key, []
    return join_key, indexes[join_key].get((name_key, mfr_key), [])


def iqvia_candidates(
    context: dict[str, Any],
    indexes: dict[str, dict[tuple[str, ...], list[dict[str, Any]]]],
) -> tuple[str | None, list[dict[str, Any]]]:
    smid = str(context["strategic_market_id"])
    join_key = IQVIA_JOIN_KEY_BY_SMID.get(smid)
    if join_key is None:
        return None, []
    atc = normalize_key(context.get("atc4_code"))
    molecule = normalize_key(context.get("molecule"))
    mfr = manufacturer_key(context.get("manufacturer"))
    pack = normalize_key(context.get("pack_desc") or context.get("strength"))

    if join_key == "iqvia_pack_manufacturer_atc4":
        candidates = indexes[join_key].get((pack, mfr, atc), []) if pack and mfr and atc else []
        if candidates:
            return join_key, candidates
        # Fall back to manufacturer+ATC+molecule for rows where pack text
        # differs between the sheet and the latest IQVIA partition.
        fallback_key = "iqvia_manufacturer_atc4_molecule"
        return f"{join_key}->fallback_mfr_atc_molecule", (
            indexes[fallback_key].get((mfr, atc, molecule), []) if mfr and atc and molecule else []
        )
    if join_key == "iqvia_manufacturer_atc4_molecule":
        return join_key, indexes[join_key].get((mfr, atc, molecule), []) if mfr and atc and molecule else []
    if join_key == "iqvia_atc4_molecule":
        return join_key, indexes[join_key].get((atc, molecule), []) if atc and molecule else []
    raise ValueError(f"unknown IQVIA join key: {join_key}")
