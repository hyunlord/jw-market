from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any

import pandas as pd

from pipeline.etl.io.catalog._common import read_parquet_rows
from pipeline.etl.io.catalog.dim_market_competitive_dynamics import CD_SPECS, filter_master_drug_rows
from pipeline.etl.io.catalog.raw_sources import partition_label_from_path
from pipeline.etl.io.catalog.target_priority_sales import (
    build_rankings_by_cd_source,
    load_iqvia_sales,
    load_ubist_sales,
)
from pipeline.etl.io.catalog.target_priority_schema import (
    AUTO_FILL_CACHE_COLUMNS,
    DIM_MARKET_TARGET_PRIORITY_COLUMNS,
    EXPECTED_BOTH_SOURCE_VIEW_CDS,
    EXPECTED_ROW_COUNT,
    EXPECTED_SOURCE_FILE_VERSION,
    EXPECTED_SOURCE_TYPE_COUNTS,
    EXPECTED_SOURCE_VIEW_COUNTS,
    LEGACY_SKELETON_SOURCE_FILE_VERSION,
)
from pipeline.etl.io.catalog.target_priority_validation import validate_records
from pipeline.etl.io.catalog.target_priority_text import (
    clean,
    customer_compare_key,
    normalize_text,
    utc_now_text,
)

def read_required_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"required CSV not found: {path}")
    return pd.read_csv(path)


def source_file_version_from_skeleton(skeleton: pd.DataFrame) -> str:
    versions = {
        unicodedata.normalize("NFC", str(value))
        for value in skeleton["source_file_version"].dropna().unique().tolist()
    }
    allowed = {
        unicodedata.normalize("NFC", EXPECTED_SOURCE_FILE_VERSION),
        unicodedata.normalize("NFC", LEGACY_SKELETON_SOURCE_FILE_VERSION),
    }
    if not versions or not versions.issubset(allowed):
        raise ValueError(
            f"source_file_version mismatch: expected one of {sorted(allowed)!r}, "
            f"actual={sorted(versions)}"
        )
    return EXPECTED_SOURCE_FILE_VERSION


def spec_by_cd_id() -> dict[str, dict[str, Any]]:
    return {str(spec["competitive_dynamics_id"]): spec for spec in CD_SPECS}


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


def write_cache(cache_rows: list[dict[str, str | None]], cache_path: Path) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(cache_rows, columns=AUTO_FILL_CACHE_COLUMNS).to_csv(cache_path, index=False)
