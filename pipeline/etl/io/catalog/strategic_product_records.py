from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipeline.etl.io.catalog._common import read_parquet_rows
from pipeline.etl.io.catalog.strategic_product_context import load_context_by_brand_id
from pipeline.etl.io.catalog.strategic_product_indexes import iqvia_candidates, load_iqvia_indexes, load_ubist_indexes, ubist_candidates, unique_candidates
from pipeline.etl.io.catalog.strategic_product_schema import EXPECTED_COLUMNS
from pipeline.etl.io.catalog.strategic_product_text import clean_text, is_sheet_product_grain, make_product_name, ml_index_from_brand_id, sheet_product_name, source_order_for_data_source, source_row_id_from_brand_id
from pipeline.etl.io.catalog.strategic_product_validation import validate_records


def utc_now_datetime() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)

def product_record_from_candidate(
    brand_row: dict[str, Any],
    context: dict[str, Any],
    candidate: dict[str, Any] | None,
    product_id: str,
    ingested_at: datetime,
) -> dict[str, Any]:
    if candidate is None:
        return {
            "product_id": product_id,
            "name": str(brand_row["name"]),
            "merge_name": str(brand_row["merge_name"]),
            "brand_id": str(brand_row["brand_id"]),
            "ml_id": str(brand_row["ml_id"]),
            "cd_id": brand_row.get("cd_id"),
            "class": brand_row.get("class"),
            "molecule": brand_row.get("molecule"),
            "molecule_raw": None,
            "dosage_form": brand_row.get("dosage_form"),
            "dosage_form_raw": None,
            "strength_pack": brand_row.get("strength_pack"),
            "nhi_type": brand_row.get("nhi_type"),
            "ox_gx": brand_row.get("ox_gx"),
            "fish_oil": brand_row.get("fish_oil"),
            "판매사": brand_row.get("판매사"),
            "제조사": brand_row.get("제조사"),
            "source_file_version": str(brand_row["source_file_version"]),
            "ingested_at": ingested_at,
        }

    source_name = make_product_name(
        candidate.get("product_name"),
        candidate.get("pack_desc") if candidate.get("source_view") == "IQVIA" else None,
    ) or str(brand_row["name"])
    # product row는 SKU granularity를 보존해야 하므로 raw molecule/dosage를
    # metadata로 남긴다. display molecule/dosage는 brand catalog가 260518
    # MI Master recode를 적용한 값이 있으면 그 값을 우선한다. raw 후보 값을
    # display에 먼저 쓰는 대안은 리바로젯/제이클처럼 MI Master display truth를
    # 다시 오염시키므로 기각했다.
    molecule_raw = clean_text(candidate.get("molecule"))
    dosage_form_raw = clean_text(candidate.get("dosage_form"))
    return {
        "product_id": product_id,
        "name": source_name,
        "merge_name": str(brand_row["merge_name"]),
        "brand_id": str(brand_row["brand_id"]),
        "ml_id": str(brand_row["ml_id"]),
        "cd_id": brand_row.get("cd_id"),
        "class": brand_row.get("class"),
        "molecule": clean_text(brand_row.get("molecule")) or molecule_raw,
        "molecule_raw": molecule_raw,
        "dosage_form": clean_text(brand_row.get("dosage_form")) or dosage_form_raw,
        "dosage_form_raw": dosage_form_raw,
        "strength_pack": clean_text(brand_row.get("strength_pack")) or clean_text(candidate.get("strength_pack")),
        "nhi_type": clean_text(candidate.get("nhi_type")) or brand_row.get("nhi_type"),
        "ox_gx": brand_row.get("ox_gx"),
        "fish_oil": brand_row.get("fish_oil"),
        "판매사": brand_row.get("판매사"),
        "제조사": clean_text(candidate.get("manufacturer")) or brand_row.get("제조사"),
        "source_file_version": str(brand_row["source_file_version"]),
        "ingested_at": ingested_at,
    }


def product_record_from_sheet_product(
    brand_row: dict[str, Any],
    product_id: str,
    ingested_at: datetime,
) -> dict[str, Any]:
    return {
        "product_id": product_id,
        "name": sheet_product_name(brand_row),
        "merge_name": str(brand_row["merge_name"]),
        "brand_id": str(brand_row["brand_id"]),
        "ml_id": str(brand_row["ml_id"]),
        "cd_id": brand_row.get("cd_id"),
        "class": brand_row.get("class"),
        "molecule": brand_row.get("molecule"),
        "molecule_raw": None,
        "dosage_form": brand_row.get("dosage_form"),
        "dosage_form_raw": None,
        "strength_pack": brand_row.get("strength_pack"),
        "nhi_type": brand_row.get("nhi_type"),
        "ox_gx": brand_row.get("ox_gx"),
        "fish_oil": brand_row.get("fish_oil"),
        "판매사": brand_row.get("판매사"),
        "제조사": brand_row.get("제조사"),
        "source_file_version": str(brand_row["source_file_version"]),
        "ingested_at": ingested_at,
    }

def load_strategic_product_records(
    strategic_brand_path: Path,
    ml_market_path: Path,
    cd_market_path: Path,
    ubist_path: Path,
    iqvia_path: Path,
    ingested_at: datetime | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    brand_rows = read_parquet_rows(strategic_brand_path)
    ml_rows = read_parquet_rows(ml_market_path)
    cd_rows = read_parquet_rows(cd_market_path)
    ml_by_id = {str(row["ml_id"]): row for row in ml_rows}
    contexts = load_context_by_brand_id()
    ubist_indexes = load_ubist_indexes(ubist_path)
    iqvia_indexes = load_iqvia_indexes(iqvia_path)
    timestamp = ingested_at or utc_now_datetime()

    records: list[dict[str, Any]] = []
    coverage_rows: list[dict[str, Any]] = []

    for brand_row in brand_rows:
        brand_id = str(brand_row["brand_id"])
        context = contexts.get(brand_id)
        if context is None:
            raise ValueError(f"missing source context for brand_id={brand_id}")
        data_source = str(ml_by_id[str(brand_row["ml_id"])]["data_source"])
        matched_candidates: list[dict[str, Any]] = []
        join_keys: list[str] = []
        source_views: list[str] = []

        if is_sheet_product_grain(brand_row, context):
            product_id = f"sp_{ml_index_from_brand_id(brand_id):03d}_{source_row_id_from_brand_id(brand_id):05d}_001"
            records.append(product_record_from_sheet_product(brand_row, product_id, timestamp))
            match_status = "sheet_product"
            matched_count = 1
            sample_names = sheet_product_name(brand_row)
            join_keys.append("sheet_product_1_to_1")
        else:
            for source_view in source_order_for_data_source(data_source):
                if source_view == "UBIST":
                    join_key, candidates = ubist_candidates(brand_row, context, ubist_indexes)
                else:
                    join_key, candidates = iqvia_candidates(context, iqvia_indexes)
                if join_key:
                    join_keys.append(join_key)
                if candidates:
                    source_views.append(source_view)
                    matched_candidates.extend(candidates)

            # De-duplicate across BOTH branches by source view and final product
            # identity so the same branch cannot inflate product rows.
            matched_candidates = unique_candidates(
                matched_candidates,
                ("source_view", "product_name", "pack_desc", "manufacturer", "strength_pack", "dosage_form", "nhi_type"),
            )

            if not matched_candidates:
                product_id = f"sp_{ml_index_from_brand_id(brand_id):03d}_{source_row_id_from_brand_id(brand_id):05d}_001"
                records.append(
                    product_record_from_candidate(brand_row, context, None, product_id, timestamp)
                )
                match_status = "fallback"
                matched_count = 0
                sample_names = ""
            else:
                for seq, candidate in enumerate(matched_candidates, start=1):
                    product_id = (
                        f"sp_{ml_index_from_brand_id(brand_id):03d}_"
                        f"{source_row_id_from_brand_id(brand_id):05d}_{seq:03d}"
                    )
                    records.append(
                        product_record_from_candidate(brand_row, context, candidate, product_id, timestamp)
                    )
                match_status = "matched"
                matched_count = len(matched_candidates)
                sample_names = " | ".join(
                    clean_text(candidate.get("product_name")) or ""
                    for candidate in matched_candidates[:5]
                )

        coverage_rows.append(
            {
                "brand_id": brand_id,
                "strategic_market_id": context["strategic_market_id"],
                "ml_id": brand_row["ml_id"],
                "cd_id": brand_row.get("cd_id") or "",
                "brand_name": brand_row["name"],
                "data_source": data_source,
                "join_keys_attempted": ";".join(join_keys),
                "source_views_matched": ";".join(source_views),
                "match_status": match_status,
                "matched_product_count": matched_count,
                "sample_product_names": sample_names,
            }
        )

    validate_records(records, coverage_rows, brand_rows, ml_rows, cd_rows)
    return records, coverage_rows
