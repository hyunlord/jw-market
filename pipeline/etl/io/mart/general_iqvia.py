from __future__ import annotations

import json
import os
from typing import Any

import duckdb
import pandas as pd

from .brand_key_normalize import best_name, normalize_brand_name
from .general_catalog import _attach_catalog
from .general_config import LOGGER, enriched_glob, iqvia_nsa_glob, mariadb_connect
from .general_utils import iqvia_source_priority, normalise_iqvia_channel, normalize_period_label, safe_float
from ..iqvia_numeric import numeric_or_comma_string_to_double_sql

def load_iqvia_base_frame(max_rows: int | None = None) -> pd.DataFrame:
    if os.environ.get("S4_INPUT_MODE", "raw") != "enriched":
        limit = f" LIMIT {int(max_rows)}" if max_rows else ""
        LOGGER.info("[iqvia_nsa] fetching raw rows%s", f" limit={max_rows}" if max_rows else "")
        source_database = os.environ.get("MARIADB_SOURCE_DATABASE")
        raw_table = (
            f"`{source_database.replace('`', '``')}`.iqvia_nsa_quarterly_raw"
            if source_database
            else "iqvia_nsa_quarterly_raw"
        )
        conn = mariadb_connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, source_file, source_row_no, audit_code, mfr_name, period_label, payload "
                    f"FROM {raw_table}{limit}"
                )
                rows = cur.fetchall()
        finally:
            conn.close()

        LOGGER.info("[iqvia_nsa] fetched %s raw rows; parsing JSON payloads", f"{len(rows):,}")
        parsed: list[dict[str, Any]] = []
        for idx, row in enumerate(rows, start=1):
            payload = json.loads(row["payload"])
            static = payload.get("static") or {}
            period_values = payload.get("period_values") or {}
            product_name = best_name(static.get("PRODUCT NAME KOR"), static.get("PRODUCT NAME"))
            atc_code = static.get("ATC 4 CODE") or "UNKNOWN"
            atc_desc = static.get("ATC 4 DESC")
            channel = normalise_iqvia_channel(row.get("audit_code"))
            if not channel:
                continue
            parsed.append(
                {
                    "raw_id": row.get("id"),
                    "source_file": row.get("source_file"),
                    "source_priority": iqvia_source_priority(row.get("source_file")),
                    "source_row_no": row.get("source_row_no"),
                    "audit_code": row.get("audit_code"),
                    "source": "iqvia_nsa",
                    "brand_name": product_name,
                    "brand_key": normalize_brand_name(product_name),
                    "product_name": product_name,
                    "product_code": static.get("PRODUCT NAME") or product_name,
                    "pack_desc": static.get("PACK DESC") or static.get("PACK DESCRIPTION"),
                    "strength": static.get("STRENGTH"),
                    "strength_pack": static.get("STRENGTH") or static.get("PACK DESC") or static.get("PACK DESCRIPTION"),
                    "molecule_desc": static.get("MOLECULE DESC"),
                    "molecule": static.get("MOLECULE DESC"),
                    "molecule_type": static.get("MOLECULE TYPE"),
                    "dosage_form": static.get("NFC 3 DESC") or static.get("NFC 2 DESC") or static.get("NFC 1 DESC"),
                    "nhi_type": static.get("NHI TYPE"),
                    "ox_gx": None,
                    "fish_oil": None,
                    "manufacturer": static.get("MFR NAME KOR") or row.get("mfr_name"),
                    "company": static.get("MFR NAME KOR") or row.get("mfr_name"),
                    "payload_static": static,
                    "atc4_code": atc_code,
                    "atc4_desc": atc_desc,
                    "period_yyyymm": normalize_period_label(row.get("period_label")),
                    "channel": channel,
                    "specialty": None,
                    "raw_sales": safe_float(period_values.get("Values LC")),
                    "raw_unit": safe_float(period_values.get("Units")),
                    "raw_dosage_unit": safe_float(period_values.get("Dosage Units")),
                    "raw_counting_unit": safe_float(period_values.get("Counting Units")),
                }
            )
            if idx % 500_000 == 0:
                LOGGER.info("[iqvia_nsa] parsed %s/%s raw rows", f"{idx:,}", f"{len(rows):,}")
        LOGGER.info("[iqvia_nsa] parsed %s usable channel rows", f"{len(parsed):,}")
        frame = pd.DataFrame(parsed)
        if frame.empty:
            return frame
        before = len(frame)
        dedupe_cols = [
            "period_yyyymm",
            "channel",
            "brand_key",
            "product_name",
            "product_code",
            "pack_desc",
            "molecule_desc",
            "nhi_type",
            "manufacturer",
            "atc4_code",
        ]
        frame = (
            frame.sort_values(["source_priority", "raw_id"], ascending=[False, False])
            .drop_duplicates(subset=dedupe_cols, keep="first")
            .copy()
        )
        frame["display_priority_value"] = frame["raw_sales"]
        LOGGER.info("[iqvia_nsa] de-duplicated overlapping extracts rows=%s -> %s", f"{before:,}", f"{len(frame):,}")
        return frame

    limit = f"LIMIT {int(max_rows)}" if max_rows else ""
    LOGGER.info("[iqvia_nsa] aggregating Layer 2 enriched parquet")
    values_lc = numeric_or_comma_string_to_double_sql("n.values_lc")
    units = numeric_or_comma_string_to_double_sql("n.units")
    dosage_units = numeric_or_comma_string_to_double_sql("n.dosage_units")
    counting_units = numeric_or_comma_string_to_double_sql("n.counting_units")
    query = f"""
        WITH enriched AS (
          SELECT *,
            split_part(source_row_id, '::', 2) AS source_file_key,
            split_part(source_row_id, '::', 3) AS sheet_name_key,
            try_cast(split_part(source_row_id, '::', 4) AS BIGINT) AS source_row_no_key,
            split_part(source_row_id, '::', 5) AS audit_code_key,
            split_part(source_row_id, '::', 6) AS period_label_key
          FROM read_parquet('{enriched_glob()}', union_by_name=true, hive_partitioning=true)
          WHERE source='nsa' AND (TRY_CAST(raw_rx_amt AS DOUBLE) > 0 OR TRY_CAST(raw_rx_cnt AS DOUBLE) > 0 OR TRY_CAST(raw_rx_qty AS DOUBLE) > 0)
          {limit}
        )
        SELECT
          e.product_id,
          e.period_yyyymm,
          e.channel,
          e.specialty,
          n.source_file,
          n.sheet_name,
          n.source_row_no,
          n.audit_code,
          first(n.product_name_kor) AS product_name_kor,
          first(n.product_name) AS product_name_en,
          first(n.pack_desc) AS pack_desc,
          first(n.strength) AS strength,
          first(n.molecule_desc) AS molecule_desc,
          first(n.molecule_type) AS molecule_type,
          first(n.nfc3_desc) AS nfc3_desc,
          first(n.nfc2_desc) AS nfc2_desc,
          first(n.nfc1_desc) AS nfc1_desc,
          first(n.nhi_type) AS nhi_type,
          first(n.mfr_name_kor) AS mfr_name_kor,
          first(n.mfr_name) AS mfr_name,
          first(n.atc4_code) AS atc4_code,
          first(n.atc4_desc) AS atc4_desc,
          SUM({values_lc}) AS raw_sales,
          SUM({units}) AS raw_unit,
          SUM({dosage_units}) AS raw_dosage_unit,
          SUM({counting_units}) AS raw_counting_unit
        FROM enriched e
        JOIN read_parquet('{iqvia_nsa_glob()}', union_by_name=true) n
          ON n.source_file = e.source_file_key
         AND n.sheet_name = e.sheet_name_key
         AND n.source_row_no = e.source_row_no_key
         AND n.audit_code = e.audit_code_key
         AND n.period_label = e.period_label_key
        GROUP BY 1,2,3,4,5,6,7,8
    """
    con = duckdb.connect()
    try:
        frame = con.execute(query).df()
    finally:
        con.close()
    if frame.empty:
        return frame
    frame = _attach_catalog(frame)
    frame["source"] = "iqvia_nsa"
    frame["display_priority_value"] = frame["raw_sales"]
    frame["brand_name"] = frame.apply(lambda row: best_name(row.get("catalog_brand_name"), row.get("product_name_kor"), row.get("product_name_en"), row.get("product_id")), axis=1)
    frame["brand_key"] = frame.apply(lambda row: best_name(row.get("catalog_brand_key"), normalize_brand_name(row.get("brand_name"))), axis=1)
    frame["product_name"] = frame.apply(lambda row: best_name(row.get("catalog_product_name"), row.get("product_name_kor"), row.get("product_name_en"), row.get("product_id")), axis=1)
    frame["product_code"] = frame.apply(lambda row: best_name(row.get("product_name_en"), row.get("product_name")), axis=1)
    frame["strength_pack"] = frame.apply(lambda row: best_name(row.get("strength"), row.get("pack_desc"), row.get("strength_pack")), axis=1)
    frame["molecule"] = frame.apply(lambda row: best_name(row.get("molecule"), row.get("molecule_desc")), axis=1)
    frame["molecule_type"] = frame["molecule_type"].where(frame["molecule_type"].notna(), None)
    frame["dosage_form"] = frame.apply(lambda row: best_name(row.get("dosage_form"), row.get("nfc3_desc"), row.get("nfc2_desc"), row.get("nfc1_desc")), axis=1)
    frame["manufacturer"] = frame.apply(lambda row: best_name(row.get("manufacturer"), row.get("mfr_name_kor"), row.get("mfr_name")), axis=1)
    frame["company"] = frame.apply(lambda row: best_name(row.get("company"), row.get("mfr_name_kor"), row.get("mfr_name")), axis=1)
    frame["atc4_code"] = frame.apply(lambda row: best_name(row.get("atc4_code"), row.get("catalog_atc4_code"), "UNKNOWN"), axis=1)
    frame["atc4_desc"] = frame["atc4_desc"].where(frame["atc4_desc"].notna(), None)
    return frame

def iqvia_measure_frame(base: pd.DataFrame, measure: str) -> pd.DataFrame:
    frame = base.copy()
    value_col = {
        "sales": "raw_sales",
        "unit": "raw_unit",
        "dosage_unit": "raw_dosage_unit",
        "counting_unit": "raw_counting_unit",
    }[measure]
    frame["measure"] = measure
    frame["raw_value"] = frame[value_col]
    return frame.loc[frame["raw_value"].notna() & (frame["raw_value"] > 0)].copy()
