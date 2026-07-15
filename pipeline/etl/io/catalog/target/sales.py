from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from pipeline.etl.io.catalog.dim.market_competitive_dynamics import filter_master_drug_rows
from pipeline.etl.io.catalog._lib.raw_sources import read_parquet_compat
from pipeline.etl.io.catalog.target.schema import UBIST_JOIN_VALUE_COLUMN_BY_SMID
from pipeline.etl.io.catalog.target.text import (
    clean,
    iqvia_customer_label,
    normalize_manufacturer,
    normalize_text,
    ubist_customer_label,
)
from pipeline.etl.io.ubist_specialties import aggregate_specialty_labels

def selected_master_rows(
    cd_id: str,
    specs_by_cd: dict[str, dict[str, Any]],
    master_drug_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if cd_id not in specs_by_cd:
        raise ValueError(f"unknown competitive_dynamics_id: {cd_id}")
    return filter_master_drug_rows(specs_by_cd[cd_id], master_drug_rows)


def load_ubist_sales(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"UBIST latest partition missing: {path}")
    columns = [
        "channel",
        "specialty",
        "manufacturer",
        "product_name",
        "brand",
        "val",
    ]
    sales = read_parquet_compat(
        path,
        columns,
        {
            "channel": "종별",
            "specialty": "진료과",
            "manufacturer": "제조사",
            "product_name": "제품",
            "brand": "브랜드",
            "val": "rx_amt",
        },
    )
    aggregate_mask = sales["specialty"].astype(str).str.strip().isin(
        aggregate_specialty_labels()
    )
    sales = sales.loc[~aggregate_mask].copy()
    sales["_brand_key"] = sales["brand"].map(normalize_text)
    sales["_product_key"] = sales["product_name"].map(normalize_text)
    sales["_manufacturer_key"] = sales["manufacturer"].map(normalize_manufacturer)
    sales["_sales"] = pd.to_numeric(sales["val"], errors="coerce").fillna(0)
    sales["_customer"] = [
        ubist_customer_label(channel, specialty)
        for channel, specialty in zip(sales["channel"], sales["specialty"])
    ]
    return sales


def load_iqvia_sales(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"IQVIA latest partition missing: {path}")
    columns = [
        "audit_code",
        "mfr_name_kor",
        "atc4_code",
        "molecule_desc",
        "pack_desc",
        "values_lc",
    ]
    sales = pd.read_parquet(path, columns=columns)
    sales["_pack_key"] = sales["pack_desc"].map(normalize_text)
    sales["_manufacturer_key"] = sales["mfr_name_kor"].map(normalize_manufacturer)
    sales["_atc_key"] = sales["atc4_code"].map(normalize_text)
    sales["_molecule_key"] = sales["molecule_desc"].map(normalize_text)
    sales["_sales"] = pd.to_numeric(sales["values_lc"], errors="coerce").fillna(0)
    sales["_customer"] = sales["audit_code"].map(iqvia_customer_label)
    return sales


def ubist_rankings(
    cd_id: str,
    specs_by_cd: dict[str, dict[str, Any]],
    master_drug_rows: list[dict[str, Any]],
    ubist_sales: pd.DataFrame,
) -> tuple[list[dict[str, Any]], str, int]:
    rows = selected_master_rows(cd_id, specs_by_cd, master_drug_rows)
    if not rows:
        return [], "ubist_no_master_rows", 0

    strategic_market_id = str(specs_by_cd[cd_id]["strategic_market_id"])
    join_value_column = UBIST_JOIN_VALUE_COLUMN_BY_SMID.get(strategic_market_id, "brand")
    sales_key_column = "_brand_key" if join_value_column == "brand" else "_product_key"
    join_key_name = f"ubist_{join_value_column}_manufacturer"

    pair_to_min_drug_index: dict[tuple[str, str], int] = {}
    for row in rows:
        product_name_key = normalize_text(row.get("product_name"))
        manufacturer_key = normalize_manufacturer(row.get("manufacturer"))
        if not product_name_key or not manufacturer_key:
            continue
        key = (product_name_key, manufacturer_key)
        drug_index = int(str(row["drug_index"]))
        pair_to_min_drug_index[key] = min(drug_index, pair_to_min_drug_index.get(key, drug_index))

    if not pair_to_min_drug_index:
        return [], join_key_name, 0

    keys = set(pair_to_min_drug_index)
    pairs = list(zip(ubist_sales[sales_key_column], ubist_sales["_manufacturer_key"]))
    matched = ubist_sales[pd.Series(pairs, index=ubist_sales.index).isin(keys)].copy()
    if matched.empty:
        return [], join_key_name, 0

    matched["_matched_drug_index"] = [
        pair_to_min_drug_index[(value_key, manufacturer_key)]
        for value_key, manufacturer_key in zip(matched[sales_key_column], matched["_manufacturer_key"])
    ]
    grouped = (
        matched.groupby("_customer", dropna=False)
        .agg(
            sales_amount=("_sales", "sum"),
            sales_rows=("_sales", "size"),
            tie_break_drug_index=("_matched_drug_index", "min"),
        )
        .reset_index()
        .sort_values(
            ["sales_amount", "tie_break_drug_index", "_customer"],
            ascending=[False, True, True],
        )
    )
    rankings = [
        {
            "target_customer": str(row["_customer"]),
            "sales_amount": float(row["sales_amount"]),
            "sales_rows": int(row["sales_rows"]),
            "tie_break_drug_index": int(row["tie_break_drug_index"]),
        }
        for row in grouped.to_dict("records")
    ]
    return rankings, join_key_name, int(len(matched))


def iqvia_rankings(
    cd_id: str,
    specs_by_cd: dict[str, dict[str, Any]],
    master_drug_rows: list[dict[str, Any]],
    iqvia_sales: pd.DataFrame,
) -> tuple[list[dict[str, Any]], str, int]:
    rows = selected_master_rows(cd_id, specs_by_cd, master_drug_rows)
    if not rows:
        return [], "iqvia_no_master_rows", 0

    pack_mfr_atc: set[tuple[str, str, str]] = set()
    mfr_atc_molecule: set[tuple[str, str, str]] = set()
    atc_molecule: set[tuple[str, str]] = set()
    for row in rows:
        pack_key = normalize_text(row.get("pack_desc"))
        manufacturer_key = normalize_manufacturer(row.get("manufacturer"))
        atc_key = normalize_text(row.get("atc4_code"))
        molecule_key = normalize_text(row.get("molecule"))
        if pack_key and manufacturer_key and atc_key:
            pack_mfr_atc.add((pack_key, manufacturer_key, atc_key))
        if manufacturer_key and atc_key and molecule_key:
            mfr_atc_molecule.add((manufacturer_key, atc_key, molecule_key))
        if atc_key and molecule_key:
            atc_molecule.add((atc_key, molecule_key))

    if not any((pack_mfr_atc, mfr_atc_molecule, atc_molecule)):
        return [], "iqvia_no_join_keys", 0

    pack_key_series = pd.Series(
        list(zip(iqvia_sales["_pack_key"], iqvia_sales["_manufacturer_key"], iqvia_sales["_atc_key"])),
        index=iqvia_sales.index,
    )
    mfr_molecule_series = pd.Series(
        list(zip(iqvia_sales["_manufacturer_key"], iqvia_sales["_atc_key"], iqvia_sales["_molecule_key"])),
        index=iqvia_sales.index,
    )
    molecule_series = pd.Series(
        list(zip(iqvia_sales["_atc_key"], iqvia_sales["_molecule_key"])),
        index=iqvia_sales.index,
    )
    mask = (
        pack_key_series.isin(pack_mfr_atc)
        | mfr_molecule_series.isin(mfr_atc_molecule)
        | molecule_series.isin(atc_molecule)
    )
    matched = iqvia_sales[mask].copy()
    if matched.empty:
        return [], "iqvia_pack_mfr_atc_or_filter_grain", 0

    grouped = (
        matched.groupby("_customer", dropna=False)
        .agg(
            sales_amount=("_sales", "sum"),
            sales_rows=("_sales", "size"),
        )
        .reset_index()
        .sort_values(["sales_amount", "_customer"], ascending=[False, True])
    )
    rankings = [
        {
            "target_customer": str(row["_customer"]),
            "sales_amount": float(row["sales_amount"]),
            "sales_rows": int(row["sales_rows"]),
            "tie_break_drug_index": None,
        }
        for row in grouped.to_dict("records")
    ]
    return rankings, "iqvia_pack_mfr_atc_or_filter_grain", int(len(matched))


def build_rankings_by_cd_source(
    auto_rows: pd.DataFrame,
    specs_by_cd: dict[str, dict[str, Any]],
    master_drug_rows: list[dict[str, Any]],
    ubist_sales: pd.DataFrame,
    iqvia_sales: pd.DataFrame,
    ubist_partition: str,
    iqvia_partition: str,
) -> dict[tuple[str, str], dict[str, Any]]:
    rankings: dict[tuple[str, str], dict[str, Any]] = {}
    for cd_id, source_view in sorted(
        set(zip(auto_rows["competitive_dynamics_id"], auto_rows["source_view"]))
    ):
        if source_view == "UBIST":
            ranked, join_key, matched_sales_rows = ubist_rankings(
                str(cd_id), specs_by_cd, master_drug_rows, ubist_sales
            )
            partition = ubist_partition
            basis = "UBIST val sum by estimated channel x specialty customer"
        elif source_view == "IQVIA":
            ranked, join_key, matched_sales_rows = iqvia_rankings(
                str(cd_id), specs_by_cd, master_drug_rows, iqvia_sales
            )
            partition = iqvia_partition
            basis = "IQVIA values_lc sum by estimated audit_code customer"
        else:
            raise ValueError(f"unexpected source_view: {source_view}")
        rankings[(str(cd_id), str(source_view))] = {
            "ranked": ranked,
            "join_key": join_key,
            "matched_sales_rows": matched_sales_rows,
            "source_partition": partition,
            "ranking_basis": basis,
        }
    return rankings
