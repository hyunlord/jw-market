"""
prototype_16_dim_market_target_priority_to_parquet.py
======================================================
Phase 12 Round 6 dim_market_target_priority -> Parquet.

Inputs:
- data/cache/prototype_11_step_c4_target_priority_precompute_sample.csv
- parquet/dim_market_competitive_dynamics/dim_market_competitive_dynamics.parquet
- parquet/master_drug/master_drug.parquet
- parquet/ubist/2026-02.parquet
- parquet/iqvia_nsa/2025-Q4.parquet

Output:
- parquet/dim_market_target_priority/dim_market_target_priority.parquet
- data/cache/prototype_12_round6_auto_fill_customer_dictionary_estimate.csv

Policy:
- This table is newly defined by Phase 11 Step C-4/C-4b and adjusted by Q-38.
- Grain is one target priority slot per CD market, source_view, and rank.
- BOTH markets are stored as source-specific rows, not merged.
- Raw slots come from R54-R57.
- Auto-fill slots use latest-partition source-specific sales ranking with the
  Q-31 estimated customer dictionary. Exact customer dictionary hardening is a
  Phase 13+ item.
- All output columns are string dtype.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import pandas as pd
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError as e:
    sys.exit(f"ERROR: {e}\n  pip3 install pandas pyarrow --break-system-packages")

try:
    from prototype_15_dim_market_competitive_dynamics_to_parquet import (
        CD_SPECS,
        clean_text,
        filter_master_drug_rows,
    )
except ImportError as e:
    sys.exit(f"ERROR: cannot import Round 5 helpers: {e}")


DEFAULT_SKELETON_FILE = Path(
    "data/cache/prototype_11_step_c4_target_priority_precompute_sample.csv"
)
DEFAULT_DIM_COMPETITIVE_FILE = Path(
    "parquet/dim_market_competitive_dynamics/dim_market_competitive_dynamics.parquet"
)
DEFAULT_MASTER_DRUG_FILE = Path("parquet/master_drug/master_drug.parquet")
DEFAULT_UBIST_BASE_DIR = Path("output/ubist")
DEFAULT_IQVIA_DIR = Path("output/iqvia_nsa")
DEFAULT_OUTPUT_FILE = Path(
    "parquet/dim_market_target_priority/dim_market_target_priority.parquet"
)
DEFAULT_CACHE_FILE = Path(
    "data/cache/prototype_12_round6_auto_fill_customer_dictionary_estimate.csv"
)

EXPECTED_SOURCE_FILE_VERSION = "MI팀_시장분석 AI_시장 분석 Master Version (260422).xlsx"
EXPECTED_ROW_COUNT = 84
EXPECTED_SOURCE_VIEW_COUNTS = {"UBIST": 40, "IQVIA": 44}
EXPECTED_SOURCE_TYPE_COUNTS = {
    "raw_from_sheet": 49,
    "auto_fill_top_n_by_sales": 35,
}
EXPECTED_BOTH_SOURCE_VIEW_CDS = {"cd_003", "cd_017"}

DIM_MARKET_TARGET_PRIORITY_COLUMNS = (
    "target_priority_id",
    "competitive_dynamics_id",
    "source_view",
    "priority_rank",
    "target_customer",
    "source_type",
    "source_evidence",
    "source_file_version",
    "ingested_at",
)

AUTO_FILL_CACHE_COLUMNS = (
    "target_priority_id",
    "competitive_dynamics_id",
    "source_view",
    "priority_rank",
    "sales_rank",
    "target_customer",
    "rank_available",
    "source_partition",
    "ranking_basis",
    "join_key",
    "sales_amount",
    "sales_rows",
    "matched_sales_rows",
    "available_customer_groups",
    "estimate_status",
    "source_evidence",
)

UBIST_LATEST_PARTITION = "latest"
IQVIA_LATEST_PARTITION = "latest"

# Q-34 / C-4b source-specific dictionary.
UBIST_JOIN_VALUE_COLUMN_BY_SMID = {
    "strategy_001": "brand",
    "strategy_003": "brand",
    "strategy_005": "brand",
    "strategy_006": "product_name",
    "strategy_007": "product_name",
    "strategy_008": "brand",
    "strategy_009": "brand",
    "strategy_015": "brand",
}


def utc_now_text() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ", timespec="seconds")


def dumps_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def clean(value: Any) -> str | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return None
    return text


def normalize_text(value: Any) -> str:
    text = clean(value)
    if text is None:
        return ""
    text = unicodedata.normalize("NFC", text)
    text = re.sub(r"\s+", "", text)
    return text.upper()


def normalize_manufacturer(value: Any) -> str:
    normalized = normalize_text(value)
    aliases = {
        normalize_text("제이더블유중외제약"): normalize_text("JW중외제약"),
    }
    return aliases.get(normalized, normalized)


def read_parquet_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"required parquet not found: {path}")
    return pq.read_table(path).to_pylist()


def read_required_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"required CSV not found: {path}")
    return pd.read_csv(path)


def resolve_ubist_latest(base_dir: Path = DEFAULT_UBIST_BASE_DIR) -> Path:
    parts = sorted(base_dir.glob("year=*/month=*/data.parquet"))
    if not parts:
        raise FileNotFoundError(f"no UBIST parquet partitions under {base_dir}")
    return parts[-1]


def resolve_iqvia_latest(base_dir: Path = DEFAULT_IQVIA_DIR) -> Path:
    parts = sorted(base_dir.glob("*.parquet"))
    if not parts:
        raise FileNotFoundError(f"no IQVIA NSA parquet partitions under {base_dir}")
    return parts[-1]


def partition_label_from_path(path: Path) -> str:
    parts = path.parts
    year = next((part.split("=", 1)[1] for part in parts if part.startswith("year=")), None)
    month = next((part.split("=", 1)[1] for part in parts if part.startswith("month=")), None)
    if year and month:
        return f"{year}-{month}"
    return path.stem


def read_parquet_compat(path: Path, columns: list[str], aliases: dict[str, str]) -> pd.DataFrame:
    schema_names = set(pq.read_schema(path).names)
    if set(columns).issubset(schema_names):
        return pd.read_parquet(path, columns=columns)
    source_columns = [aliases.get(column, column) for column in columns]
    missing = [column for column in source_columns if column not in schema_names]
    if missing:
        raise ValueError(f"{path} missing columns for compatibility read: {missing}")
    frame = pd.read_parquet(path, columns=source_columns)
    return frame.rename(columns={source: target for target, source in aliases.items()})


def source_file_version_from_skeleton(skeleton: pd.DataFrame) -> str:
    versions = {
        unicodedata.normalize("NFC", str(value))
        for value in skeleton["source_file_version"].dropna().unique().tolist()
    }
    if versions != {EXPECTED_SOURCE_FILE_VERSION}:
        raise ValueError(
            f"source_file_version mismatch: expected={EXPECTED_SOURCE_FILE_VERSION!r}, "
            f"actual={sorted(versions)}"
        )
    return EXPECTED_SOURCE_FILE_VERSION


def spec_by_cd_id() -> dict[str, dict[str, Any]]:
    return {str(spec["competitive_dynamics_id"]): spec for spec in CD_SPECS}


def ubist_customer_label(channel: Any, specialty: Any) -> str:
    channel_text = clean(channel) or ""
    specialty_text = clean(specialty) or ""
    prefix = "CL" if channel_text == "의원" else "GH"

    if "순환기" in specialty_text:
        suffix = "Cardio"
    elif "내분비" in specialty_text:
        suffix = "Endo"
    elif "신경과" in specialty_text:
        suffix = "Neuro"
    elif "비뇨" in specialty_text:
        suffix = "Uro"
    elif "소화기" in specialty_text:
        suffix = "GI"
    elif "신장" in specialty_text:
        suffix = "Nephro"
    elif "혈액종양" in specialty_text:
        suffix = "Hemato"
    elif "일반의" in specialty_text or "가정의학" in specialty_text or specialty_text == "내과(IM)":
        suffix = "IGF"
    elif "정형" in specialty_text:
        suffix = "OS"
    elif "소아" in specialty_text:
        suffix = "PED"
    elif "Others" in specialty_text or "unknown" in specialty_text or not specialty_text:
        suffix = "Others"
    else:
        suffix = re.sub(r"\(.+?\)", "", specialty_text).strip()[:20] or "Others"
    return f"{prefix} {suffix}"


def iqvia_customer_label(audit_code: Any) -> str:
    audit = clean(audit_code) or ""
    if audit.startswith("KCPA"):
        return "KCPA"
    if audit.startswith("KHPA"):
        return "KHPA"
    if audit.startswith("KPA"):
        return "KPA"
    return audit or "IQVIA/OTHER"


def customer_compare_key(value: Any) -> str:
    text = clean(value)
    if text is None:
        return ""
    text = unicodedata.normalize("NFC", text).strip()
    upper_text = text.upper()
    for prefix in ("IQVIA/", "UBIST/"):
        if upper_text.startswith(prefix):
            return upper_text[len(prefix):]
    return upper_text


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


def auto_fill_value(
    skeleton_row: dict[str, Any],
    auto_assignments: dict[str, dict[str, Any]],
) -> tuple[str | None, str, dict[str, str | None]]:
    cd_id = str(skeleton_row["competitive_dynamics_id"])
    source_view = str(skeleton_row["source_view"])
    priority_rank = int(str(skeleton_row["priority_rank"]))
    assignment = auto_assignments.get(str(skeleton_row["target_priority_id"]))
    if assignment is None:
        raise ValueError(f"missing auto-fill assignment for {skeleton_row['target_priority_id']}")
    available_groups = int(assignment["available_customer_groups"])
    base_cache = {
        "target_priority_id": str(skeleton_row["target_priority_id"]),
        "competitive_dynamics_id": cd_id,
        "source_view": source_view,
        "priority_rank": str(priority_rank),
        "sales_rank": clean(assignment.get("sales_rank")),
        "source_partition": str(assignment["source_partition"]),
        "ranking_basis": str(assignment["ranking_basis"]),
        "join_key": str(assignment["join_key"]),
        "matched_sales_rows": str(assignment["matched_sales_rows"]),
        "available_customer_groups": str(available_groups),
    }

    if assignment["target_customer"] is not None:
        customer = str(assignment["target_customer"])
        evidence = (
            f"auto-fill priority rank {priority_rank}; source={source_view}; "
            f"partition={assignment['source_partition']}; "
            f"basis={assignment['ranking_basis']}; join_key={assignment['join_key']}; "
            f"sales_rank={assignment['sales_rank']}; "
            f"sales_amount={assignment['sales_amount']:.2f}; "
            f"sales_rows={assignment['sales_rows']}; "
            f"available_customer_groups={available_groups}; Q-31 estimated dictionary"
        )
        cache_row = {
            **base_cache,
            "target_customer": customer,
            "rank_available": "true",
            "sales_amount": f"{assignment['sales_amount']:.2f}",
            "sales_rows": str(assignment["sales_rows"]),
            "estimate_status": "materialized_from_latest_partition",
            "source_evidence": evidence,
        }
        return customer, evidence, cache_row

    evidence = (
        f"auto-fill priority rank {priority_rank}; source={source_view}; "
        f"partition={assignment['source_partition']}; "
        f"basis={assignment['ranking_basis']}; join_key={assignment['join_key']}; "
        f"no available customer group after excluding raw slots; "
        f"available_customer_groups={available_groups}; Q-31 exact dictionary pending"
    )
    cache_row = {
        **base_cache,
        "target_customer": None,
        "rank_available": "false",
        "sales_amount": None,
        "sales_rows": None,
        "estimate_status": "no_available_rank_in_latest_partition",
        "source_evidence": evidence,
    }
    return None, evidence, cache_row


def raw_source_evidence(skeleton_row: dict[str, Any]) -> str:
    return (
        f"raw_from_sheet R{skeleton_row['raw_row_id']} "
        f"cols={skeleton_row['raw_column_ids']}; "
        f"value={skeleton_row['raw_value_json']}"
    )


def build_auto_assignments(
    skeleton: pd.DataFrame,
    rankings_by_cd_source: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    assignments: dict[str, dict[str, Any]] = {}
    for (cd_id, source_view), group in skeleton.groupby(["competitive_dynamics_id", "source_view"]):
        ranking_info = rankings_by_cd_source.get((str(cd_id), str(source_view)))
        if ranking_info is None:
            continue
        raw_customer_keys = {
            customer_compare_key(value)
            for value in group[group["source_type"] == "raw_from_sheet"]["target_customer"].tolist()
            if customer_compare_key(value)
        }
        ranked_candidates: list[dict[str, Any]] = []
        for sales_rank, ranked_item in enumerate(ranking_info["ranked"], start=1):
            if customer_compare_key(ranked_item["target_customer"]) in raw_customer_keys:
                continue
            ranked_candidates.append({**ranked_item, "sales_rank": sales_rank})

        auto_group = group[group["source_type"] == "auto_fill_top_n_by_sales"].sort_values(
            "priority_rank", key=lambda series: series.astype(int)
        )
        for offset, row in enumerate(auto_group.to_dict("records")):
            target_priority_id = str(row["target_priority_id"])
            if offset < len(ranked_candidates):
                candidate = ranked_candidates[offset]
                assignments[target_priority_id] = {
                    **candidate,
                    "source_partition": ranking_info["source_partition"],
                    "ranking_basis": ranking_info["ranking_basis"],
                    "join_key": ranking_info["join_key"],
                    "matched_sales_rows": ranking_info["matched_sales_rows"],
                    "available_customer_groups": len(ranked_candidates),
                }
            else:
                assignments[target_priority_id] = {
                    "target_customer": None,
                    "sales_rank": None,
                    "sales_amount": None,
                    "sales_rows": None,
                    "source_partition": ranking_info["source_partition"],
                    "ranking_basis": ranking_info["ranking_basis"],
                    "join_key": ranking_info["join_key"],
                    "matched_sales_rows": ranking_info["matched_sales_rows"],
                    "available_customer_groups": len(ranked_candidates),
                }
    return assignments


def load_dim_market_target_priority_records(
    skeleton_path: Path,
    dim_competitive_path: Path,
    master_drug_path: Path,
    ubist_path: Path,
    iqvia_path: Path,
    cache_path: Path,
    ingested_at: str | None = None,
) -> list[dict[str, str | None]]:
    skeleton = read_required_csv(skeleton_path)
    source_file_version = source_file_version_from_skeleton(skeleton)
    dim_competitive_rows = read_parquet_rows(dim_competitive_path)
    master_drug_rows = read_parquet_rows(master_drug_path)
    specs_by_cd = spec_by_cd_id()

    auto_rows = skeleton[skeleton["source_type"] == "auto_fill_top_n_by_sales"]
    ubist_sales = load_ubist_sales(ubist_path)
    iqvia_sales = load_iqvia_sales(iqvia_path)
    rankings_by_cd_source = build_rankings_by_cd_source(
        auto_rows,
        specs_by_cd,
        master_drug_rows,
        ubist_sales,
        iqvia_sales,
        partition_label_from_path(ubist_path),
        partition_label_from_path(iqvia_path),
    )
    auto_assignments = build_auto_assignments(skeleton, rankings_by_cd_source)

    timestamp = ingested_at or utc_now_text()
    records: list[dict[str, str | None]] = []
    cache_rows: list[dict[str, str | None]] = []

    for row in skeleton.to_dict("records"):
        source_type = str(row["source_type"])
        target_customer = clean(row.get("target_customer"))
        if source_type == "raw_from_sheet":
            evidence = raw_source_evidence(row)
        elif source_type == "auto_fill_top_n_by_sales":
            target_customer, evidence, cache_row = auto_fill_value(row, auto_assignments)
            cache_rows.append(cache_row)
        else:
            raise ValueError(f"unexpected source_type: {source_type}")

        records.append(
            {
                "target_priority_id": str(row["target_priority_id"]),
                "competitive_dynamics_id": str(row["competitive_dynamics_id"]),
                "source_view": str(row["source_view"]),
                "priority_rank": str(int(row["priority_rank"])),
                "target_customer": target_customer,
                "source_type": source_type,
                "source_evidence": evidence,
                "source_file_version": source_file_version,
                "ingested_at": timestamp,
            }
        )

    validate_records(records, dim_competitive_rows, cache_rows)
    write_cache(cache_rows, cache_path)
    return records


def validate_records(
    records: list[dict[str, Any]],
    dim_competitive_rows: list[dict[str, Any]],
    cache_rows: list[dict[str, Any]],
) -> None:
    if len(records) != EXPECTED_ROW_COUNT:
        raise ValueError(f"row count must be {EXPECTED_ROW_COUNT}, found={len(records)}")
    for index, record in enumerate(records, start=1):
        if tuple(record.keys()) != DIM_MARKET_TARGET_PRIORITY_COLUMNS:
            raise ValueError(
                f"row {index} columns mismatch: expected={DIM_MARKET_TARGET_PRIORITY_COLUMNS}, "
                f"actual={tuple(record.keys())}"
            )
        for column, value in record.items():
            if value is not None and not isinstance(value, str):
                raise ValueError(f"row {index} column {column} must be string/None, got={type(value)}")

    target_ids = [record["target_priority_id"] for record in records]
    expected_ids = [f"tp_{index:03d}" for index in range(1, EXPECTED_ROW_COUNT + 1)]
    if target_ids != expected_ids:
        raise ValueError(f"target_priority_id sequence mismatch: {target_ids}")
    if len(set(target_ids)) != EXPECTED_ROW_COUNT:
        raise ValueError("target_priority_id must be unique")

    unique_keys = [
        (
            record["competitive_dynamics_id"],
            record["source_view"],
            record["priority_rank"],
        )
        for record in records
    ]
    if len(set(unique_keys)) != EXPECTED_ROW_COUNT:
        duplicates = [key for key, count in Counter(unique_keys).items() if count > 1]
        raise ValueError(f"(cd_id, source_view, priority_rank) duplicates: {duplicates}")

    cd_ids = {str(row["competitive_dynamics_id"]) for row in dim_competitive_rows}
    for record in records:
        if record["competitive_dynamics_id"] not in cd_ids:
            raise ValueError(f"missing competitive_dynamics FK: {record['competitive_dynamics_id']}")

    source_view_counts = dict(Counter(record["source_view"] for record in records))
    if source_view_counts != EXPECTED_SOURCE_VIEW_COUNTS:
        raise ValueError(
            f"source_view distribution mismatch: expected={EXPECTED_SOURCE_VIEW_COUNTS}, "
            f"actual={source_view_counts}"
        )
    source_type_counts = dict(Counter(record["source_type"] for record in records))
    if source_type_counts != EXPECTED_SOURCE_TYPE_COUNTS:
        raise ValueError(
            f"source_type distribution mismatch: expected={EXPECTED_SOURCE_TYPE_COUNTS}, "
            f"actual={source_type_counts}"
        )

    ranks_by_cd_source: dict[tuple[str, str], list[str]] = defaultdict(list)
    source_views_by_cd: dict[str, set[str]] = defaultdict(set)
    for record in records:
        cd_id = str(record["competitive_dynamics_id"])
        source_view = str(record["source_view"])
        ranks_by_cd_source[(cd_id, source_view)].append(str(record["priority_rank"]))
        source_views_by_cd[cd_id].add(source_view)
    for key, ranks in ranks_by_cd_source.items():
        if sorted(ranks, key=int) != ["1", "2", "3", "4"]:
            raise ValueError(f"priority_rank must be 1-4 for {key}: {ranks}")
    both_source_view_cds = {
        cd_id for cd_id, source_views in source_views_by_cd.items() if len(source_views) == 2
    }
    if both_source_view_cds != EXPECTED_BOTH_SOURCE_VIEW_CDS:
        raise ValueError(
            f"BOTH source_view CD mismatch: expected={EXPECTED_BOTH_SOURCE_VIEW_CDS}, "
            f"actual={both_source_view_cds}"
        )

    if len(cache_rows) != EXPECTED_SOURCE_TYPE_COUNTS["auto_fill_top_n_by_sales"]:
        raise ValueError(f"auto-fill cache row count mismatch: {len(cache_rows)}")
    for cache_row in cache_rows:
        if set(cache_row.keys()) != set(AUTO_FILL_CACHE_COLUMNS):
            raise ValueError(f"auto-fill cache shape mismatch: {cache_row.keys()}")


def write_cache(cache_rows: list[dict[str, str | None]], cache_path: Path) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(cache_rows, columns=AUTO_FILL_CACHE_COLUMNS).to_csv(cache_path, index=False)


def write_parquet(records: list[dict[str, str | None]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    schema = pa.schema([(column, pa.string()) for column in DIM_MARKET_TARGET_PRIORITY_COLUMNS])
    arrays = [
        pa.array([record.get(column) for record in records], type=pa.string())
        for column in DIM_MARKET_TARGET_PRIORITY_COLUMNS
    ]
    table = pa.Table.from_arrays(arrays, schema=schema)
    pq.write_table(table, output_path, compression="snappy")


def print_summary(records: list[dict[str, str | None]], cache_path: Path, output_path: Path) -> None:
    source_view_counts = Counter(record["source_view"] for record in records)
    source_type_counts = Counter(record["source_type"] for record in records)
    auto_fill_null_count = sum(
        1
        for record in records
        if record["source_type"] == "auto_fill_top_n_by_sales" and record["target_customer"] is None
    )
    print("Phase 12 Round 6 dim_market_target_priority load complete")
    print(f"- rows: {len(records)}")
    print(f"- source_view: {dict(source_view_counts)}")
    print(f"- source_type: {dict(source_type_counts)}")
    print(f"- auto_fill rows without available latest-partition rank: {auto_fill_null_count}")
    print(f"- parquet: {output_path}")
    print(f"- auto_fill dictionary cache: {cache_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skeleton", type=Path, default=DEFAULT_SKELETON_FILE)
    parser.add_argument("--dim-competitive", type=Path, default=DEFAULT_DIM_COMPETITIVE_FILE)
    parser.add_argument("--master-drug", type=Path, default=DEFAULT_MASTER_DRUG_FILE)
    parser.add_argument("--ubist", "--ubist-path", dest="ubist", type=Path, default=None)
    parser.add_argument("--iqvia", "--iqvia-path", dest="iqvia", type=Path, default=None)
    parser.add_argument("--ubist-base-dir", type=Path, default=DEFAULT_UBIST_BASE_DIR)
    parser.add_argument("--iqvia-dir", type=Path, default=DEFAULT_IQVIA_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_FILE)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE_FILE)
    parser.add_argument("--ingested-at", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    ubist_path = args.ubist or resolve_ubist_latest(args.ubist_base_dir)
    iqvia_path = args.iqvia or resolve_iqvia_latest(args.iqvia_dir)
    records = load_dim_market_target_priority_records(
        skeleton_path=args.skeleton,
        dim_competitive_path=args.dim_competitive,
        master_drug_path=args.master_drug,
        ubist_path=ubist_path,
        iqvia_path=iqvia_path,
        cache_path=args.cache,
        ingested_at=args.ingested_at,
    )
    write_parquet(records, args.output)
    print_summary(records, args.cache, args.output)


if __name__ == "__main__":
    main()
