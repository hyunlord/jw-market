#!/usr/bin/env python3
"""Verify Layer 2 enriched parquet consistency without mutating data."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import duckdb
import pandas as pd
import pyarrow.parquet as pq

from ops_utils import find_project_root


PROJECT_ROOT = find_project_root(Path(__file__).resolve())
AUDIT_DIR = PROJECT_ROOT / "docs" / "audit" / "phase_16g4_side_verify_l2"
ENRICHED_GLOB = str(PROJECT_ROOT / "output" / "enriched" / "ml_id=*" / "data.parquet")
UBIST_GLOB = str(PROJECT_ROOT / "output" / "ubist" / "year=*" / "month=*" / "data.parquet")
STRATEGIC_PRODUCT_PATH = PROJECT_ROOT / "output" / "catalog" / "strategic_product" / "strategic_product.parquet"
STRATEGIC_BRAND_PATH = PROJECT_ROOT / "output" / "catalog" / "strategic_brand" / "strategic_brand.parquet"
EXPECTED_TOTAL_ROWS = 73_300_000
EXPECTED_TOTAL_TOLERANCE = 1_000_000
JW_BRANDS = [
    "리바로",
    "리바로젯",
    "리바로브이",
    "리바로페노",
    "리바로하이",
    "페린젝트",
    "시그마트",
    "가드메트",
    "타발리스",
]
JW_BRAND_ALIASES = {
    "리바로": ["리바로 정", "리바로정"],
    "리바로젯": ["리바로젯"],
    "리바로브이": ["리바로브이", "리바로 브이"],
    "리바로페노": ["리바로페노"],
    "리바로하이": ["리바로하이", "리바로 하이"],
    "페린젝트": ["페린젝트", "FERINJECT"],
    "시그마트": ["시그마트", "SIGMART"],
    "가드메트": ["가드메트", "GUARDMET"],
    "타발리스": ["타발리스", "TAVALISSE"],
}
EXPECTED_CHANNELS = {"TH", "GH", "Semi", "CL", "기타", "Unknown", "KHPA", "KCPA", "KPA", "Sell_Out"}
EXPECTED_SPECIALTIES = {"IGF", "Cardio", "GI", "Endo", "Nephro", "Neuro", "Uro", "Unknown", ""}


def expected_ml_ids() -> list[str]:
    return [f"ml_{idx:03d}" for idx in range(1, 17)]


def partition_ml_id_from_path(path: Path) -> str:
    for part in path.parts:
        if part.startswith("ml_id="):
            return part.split("=", 1)[1]
    raise ValueError(f"not an enriched hive partition path: {path}")


def parquet_row_count(path: Path) -> int:
    return int(pq.ParquetFile(path).metadata.num_rows)


def status_for_total_l2(total_rows: int) -> str:
    return "PASS" if abs(total_rows - EXPECTED_TOTAL_ROWS) <= EXPECTED_TOTAL_TOLERANCE else "WARN"


def read_l2() -> str:
    return f"read_parquet('{ENRICHED_GLOB}', hive_partitioning=false)"


def read_ubist() -> str:
    return f"read_parquet('{UBIST_GLOB}', hive_partitioning=false)"


def discover_partitions() -> dict[str, dict[str, Any]]:
    discovered = {
        partition_ml_id_from_path(path): path
        for path in sorted((PROJECT_ROOT / "output" / "enriched").glob("ml_id=*/data.parquet"))
    }
    partitions: dict[str, dict[str, Any]] = {}
    for ml_id in expected_ml_ids():
        path = discovered.get(ml_id)
        if not path:
            partitions[ml_id] = {"exists": False, "rows": 0, "size_mb": 0}
            continue
        partitions[ml_id] = {
            "exists": True,
            "rows": parquet_row_count(path),
            "size_mb": round(path.stat().st_size / 1024 / 1024, 2),
            "path": str(path.relative_to(PROJECT_ROOT)),
        }
    return partitions


def fetch_dicts(con: duckdb.DuckDBPyConnection, sql: str) -> list[dict[str, Any]]:
    cols = [desc[0] for desc in con.execute(sql).description]
    return [dict(zip(cols, row)) for row in con.fetchall()]


def canonical_value_checks(con: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    rows = fetch_dicts(
        con,
        f"""
        SELECT
          source,
          COUNT(*) AS rows_total,
          SUM(CASE WHEN raw_rx_amt IS NULL OR canonical_value IS NULL THEN 1 ELSE 0 END) AS null_pair_rows,
          COUNT(*) FILTER (WHERE raw_rx_amt IS NOT NULL AND canonical_value IS NOT NULL) AS rows_checked,
          AVG(ABS(canonical_value - raw_rx_amt)) FILTER (WHERE raw_rx_amt IS NOT NULL AND canonical_value IS NOT NULL) AS mean_diff,
          MAX(ABS(canonical_value - raw_rx_amt)) FILTER (WHERE raw_rx_amt IS NOT NULL AND canonical_value IS NOT NULL) AS max_diff,
          SUM(CASE WHEN ABS(canonical_value - raw_rx_amt) > 0.000001 THEN 1 ELSE 0 END) AS mismatch_count
        FROM {read_l2()}
        GROUP BY source
        ORDER BY source
        """,
    )
    checks = []
    for row in rows:
        mismatch_count = int(row.get("mismatch_count") or 0)
        rows_checked = int(row.get("rows_checked") or 0)
        checks.append(
            {
                "source": row["source"],
                "rows_total": int(row["rows_total"]),
                "null_pair_rows": int(row["null_pair_rows"] or 0),
                "rows_checked": rows_checked,
                "mean_diff": float(row["mean_diff"] or 0),
                "max_diff": float(row["max_diff"] or 0),
                "mismatch_count": mismatch_count,
                "status": "PASS" if rows_checked > 0 and mismatch_count == 0 else "WARN",
            }
        )
    return checks


def product_fk_check(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    catalog_products = pd.read_parquet(STRATEGIC_PRODUCT_PATH, columns=["product_id"]).dropna().drop_duplicates()
    con.register("catalog_products", catalog_products)
    stats = con.execute(
        f"""
        WITH l2_products AS (
          SELECT DISTINCT product_id
          FROM {read_l2()}
          WHERE product_id IS NOT NULL AND product_id <> ''
        )
        SELECT
          (SELECT COUNT(*) FROM l2_products) AS l2_product_count,
          (SELECT COUNT(*) FROM catalog_products) AS catalog_product_count,
          (SELECT COUNT(*) FROM l2_products WHERE product_id NOT IN (SELECT product_id FROM catalog_products)) AS l2_not_in_catalog,
          (SELECT COUNT(*) FROM catalog_products WHERE product_id NOT IN (SELECT product_id FROM l2_products)) AS catalog_not_in_l2
        """
    ).fetchone()
    samples = con.execute(
        f"""
        WITH l2_products AS (
          SELECT DISTINCT product_id
          FROM {read_l2()}
          WHERE product_id IS NOT NULL AND product_id <> ''
        )
        SELECT product_id
        FROM l2_products
        WHERE product_id NOT IN (SELECT product_id FROM catalog_products)
        ORDER BY product_id
        LIMIT 10
        """
    ).fetchall()
    return {
        "name": "product_id FK consistency",
        "l2_product_count": int(stats[0]),
        "catalog_product_count": int(stats[1]),
        "l2_not_in_catalog": int(stats[2]),
        "catalog_not_in_l2": int(stats[3]),
        "sample_l2_not_in_catalog": [row[0] for row in samples],
        "status": "PASS" if int(stats[2]) == 0 else "FAIL",
        "note": "l2_not_in_catalog should be 0; catalog_not_in_l2 can be nonzero when catalog products have no raw match.",
    }


def channel_distribution(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    return {
        str(channel): int(count)
        for channel, count in con.execute(
            f"""
            SELECT channel, COUNT(*) AS rows
            FROM {read_l2()}
            GROUP BY channel
            ORDER BY rows DESC
            """
        ).fetchall()
    }


def specialty_distribution(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    return {
        str(specialty): int(count)
        for specialty, count in con.execute(
            f"""
            SELECT specialty, COUNT(*) AS rows
            FROM {read_l2()}
            GROUP BY specialty
            ORDER BY rows DESC
            """
        ).fetchall()
    }


def l1_l2_reconcile_ml006(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    l2_market = con.execute(
        f"""
        SELECT COUNT(*) AS rows, SUM(raw_rx_amt) AS raw_rx_amt_sum, SUM(canonical_value) AS canonical_value_sum
        FROM {read_l2()}
        WHERE ml_id = 'ml_006' AND source = 'ubist'
        """
    ).fetchone()
    l1_livaro = con.execute(
        f"""
        SELECT COUNT(*) AS rows, SUM(rx_amt) AS raw_rx_amt_sum
        FROM {read_ubist()}
        WHERE 브랜드 = '리바로'
        """
    ).fetchone()
    l2_livaro_family = con.execute(
        f"""
        WITH livaro_products AS (
          SELECT product_id
          FROM read_parquet('{STRATEGIC_PRODUCT_PATH}')
          WHERE ml_id = 'ml_006'
            AND (
              merge_name = '리바로'
              OR name LIKE '리바로%'
              OR merge_name LIKE '리바로%'
            )
        )
        SELECT COUNT(*) AS rows, SUM(raw_rx_amt) AS raw_rx_amt_sum
        FROM {read_l2()}
        WHERE ml_id = 'ml_006'
          AND source = 'ubist'
          AND product_id IN (SELECT product_id FROM livaro_products)
        """
    ).fetchone()
    return {
        "name": "L1 to L2 reconcile (ml_006 UBIST / Livaro reference)",
        "status": "INFO",
        "l2_ml006_ubist_rows": int(l2_market[0] or 0),
        "l2_ml006_ubist_raw_rx_amt_sum": float(l2_market[1] or 0),
        "l2_ml006_ubist_canonical_value_sum": float(l2_market[2] or 0),
        "l1_livaro_brand_rows": int(l1_livaro[0] or 0),
        "l1_livaro_brand_raw_rx_amt_sum": float(l1_livaro[1] or 0),
        "l2_livaro_family_rows": int(l2_livaro_family[0] or 0),
        "l2_livaro_family_raw_rx_amt_sum": float(l2_livaro_family[1] or 0),
        "note": "ml_006 L2 total is the full Livaro market, not only the raw UBIST brand='리바로' slice.",
    }


def jw_brand_tracking(con: duckdb.DuckDBPyConnection) -> dict[str, dict[str, Any]]:
    brands = pd.read_parquet(STRATEGIC_BRAND_PATH)
    products = pd.read_parquet(STRATEGIC_PRODUCT_PATH, columns=["product_id", "brand_id", "ml_id"])
    product_names = pd.read_parquet(STRATEGIC_PRODUCT_PATH, columns=["product_id", "brand_id", "ml_id", "name", "merge_name"])
    product_text = (product_names["name"].fillna("") + " " + product_names["merge_name"].fillna("")).astype(str)
    product_text_no_space = product_text.str.replace(" ", "", regex=False).str.upper()
    map_rows: list[dict[str, str]] = []
    tracking: dict[str, dict[str, Any]] = {}
    for brand in JW_BRANDS:
        brand_rows = brands[(brands["name"] == brand) | (brands["merge_name"] == brand)]
        brand_ids = sorted(str(value) for value in brand_rows["brand_id"].dropna().unique())
        product_rows = products[products["brand_id"].isin(brand_ids)] if brand_ids else products.iloc[0:0]
        match_basis = "brand_catalog_exact" if not brand_rows.empty else ""

        alias_rows = product_names.iloc[0:0]
        for alias in JW_BRAND_ALIASES.get(brand, [brand]):
            alias_no_space = alias.replace(" ", "").upper()
            alias_rows = pd.concat(
                [
                    alias_rows,
                    product_names[
                        product_text.str.contains(alias, case=False, regex=False, na=False)
                        | product_text_no_space.str.contains(alias_no_space, regex=False, na=False)
                    ],
                ],
                ignore_index=True,
            )
        if not alias_rows.empty:
            product_rows = pd.concat([product_rows, alias_rows[["product_id", "brand_id", "ml_id"]]], ignore_index=True).drop_duplicates()
            brand_ids = sorted(str(value) for value in product_rows["brand_id"].dropna().unique())
            match_basis = "brand_catalog_exact+product_alias" if match_basis else "product_alias"

        if product_rows.empty and brand_rows.empty:
            tracking[brand] = {
                "in_catalog": False,
                "brand_ids_count": 0,
                "product_ids_count": 0,
                "l2_rows": 0,
                "match_basis": "",
            }
            continue
        product_ids = sorted(str(value) for value in product_rows["product_id"].dropna().unique())
        tracking[brand] = {
            "in_catalog": True,
            "brand_ids_count": len(brand_ids),
            "product_ids_count": len(product_ids),
            "l2_rows": 0,
            "sources": {},
            "match_basis": match_basis,
        }
        for product_id in product_ids:
            map_rows.append({"brand": brand, "product_id": product_id})

    if not map_rows:
        return tracking

    con.register("jw_product_map", pd.DataFrame(map_rows).drop_duplicates())
    rows = con.execute(
        f"""
        SELECT m.brand, e.source, COUNT(*) AS rows
        FROM jw_product_map AS m
        LEFT JOIN {read_l2()} AS e
          ON m.product_id = e.product_id
        GROUP BY m.brand, e.source
        ORDER BY m.brand, e.source
        """
    ).fetchall()
    for brand, source, count in rows:
        if brand not in tracking:
            continue
        count_int = int(count or 0)
        if source is None:
            continue
        tracking[brand]["sources"][str(source)] = count_int
        tracking[brand]["l2_rows"] += count_int
    for brand, info in tracking.items():
        info["status"] = "PASS" if info.get("in_catalog") and int(info.get("l2_rows", 0)) > 0 else "WARN"
    return tracking


def verify_l2() -> dict[str, Any]:
    generated_at = datetime.now().isoformat(timespec="seconds")
    partitions = discover_partitions()
    total_l2_rows = sum(int(info["rows"]) for info in partitions.values())
    partition_count = sum(1 for info in partitions.values() if info["exists"])
    missing_partitions = [ml_id for ml_id, info in partitions.items() if not info["exists"]]
    checks: list[dict[str, Any]] = [
        {
            "name": "L2 total row count",
            "value": total_l2_rows,
            "expected_approx": EXPECTED_TOTAL_ROWS,
            "tolerance": EXPECTED_TOTAL_TOLERANCE,
            "partition_count": partition_count,
            "status": status_for_total_l2(total_l2_rows),
        },
        {
            "name": "16 ml_id partition coverage",
            "value": partition_count,
            "expected": 16,
            "missing_partitions": missing_partitions,
            "status": "PASS" if partition_count == 16 and not missing_partitions else "FAIL",
        },
    ]

    con = duckdb.connect()
    try:
        source_distribution = {
            str(source): int(rows)
            for source, rows in con.execute(
                f"""
                SELECT source, COUNT(*) AS rows
                FROM {read_l2()}
                GROUP BY source
                ORDER BY rows DESC
                """
            ).fetchall()
        }
        source_by_partition = fetch_dicts(
            con,
            f"""
            SELECT ml_id, source, COUNT(*) AS rows
            FROM {read_l2()}
            GROUP BY ml_id, source
            ORDER BY ml_id, source
            """,
        )
        source_period_distribution = fetch_dicts(
            con,
            f"""
            SELECT ml_id, source, period_yyyymm, COUNT(*) AS rows
            FROM {read_l2()}
            GROUP BY ml_id, source, period_yyyymm
            ORDER BY ml_id, source, period_yyyymm
            """,
        )

        canonical_checks = canonical_value_checks(con)
        checks.append(
            {
                "name": "canonical_value vs raw_rx_amt",
                "status": "PASS" if all(row["status"] == "PASS" for row in canonical_checks) else "WARN",
                "source_results": canonical_checks,
            }
        )

        checks.append(product_fk_check(con))

        channels = channel_distribution(con)
        observed_channels = set(channels)
        checks.append(
            {
                "name": "channel normalization",
                "status": "PASS" if not (observed_channels - EXPECTED_CHANNELS) else "WARN",
                "observed": sorted(observed_channels),
                "unexpected": sorted(observed_channels - EXPECTED_CHANNELS),
                "note": "Sell_Out is the CHSO channel emitted by Layer 2 ETL.",
            }
        )

        specialties = specialty_distribution(con)
        observed_specialties = set(specialties)
        unknown_rows = int(specialties.get("Unknown", 0))
        checks.append(
            {
                "name": "specialty normalization",
                "status": "INFO",
                "observed": sorted(observed_specialties),
                "unexpected": sorted(observed_specialties - EXPECTED_SPECIALTIES),
                "unknown_rows": unknown_rows,
                "unknown_pct": round(unknown_rows / total_l2_rows * 100, 4) if total_l2_rows else 0,
                "blank_rows": int(specialties.get("", 0)),
                "note": "Blank specialty is expected for IQVIA/CHSO rows; Unknown is expected when UBIST specialty dictionary has no mapping.",
            }
        )

        reconcile = l1_l2_reconcile_ml006(con)
        checks.append(reconcile)

        tracking = jw_brand_tracking(con)
        checks.append(
            {
                "name": "JW brand catalog to L2 tracking",
                "status": "PASS" if all(info.get("status") == "PASS" for info in tracking.values()) else "WARN",
                "brands_checked": len(tracking),
                "warn_brands": [brand for brand, info in tracking.items() if info.get("status") != "PASS"],
            }
        )
    finally:
        con.close()

    result: dict[str, Any] = {
        "phase": "16-G-4-Side-Verify-L2",
        "layer": "L2 enriched",
        "generated_at": generated_at,
        "checks": checks,
        "partition_breakdown": partitions,
        "source_distribution": source_distribution,
        "source_by_partition": source_by_partition,
        "source_period_distribution": source_period_distribution,
        "channel_distribution": channels,
        "specialty_distribution": specialties,
        "jw_brand_tracking": tracking,
        "notes": [
            "This phase reads Layer 2 parquet, Layer 1 UBIST parquet, and catalog parquet only.",
            "No DB/mart/cache/ETL/migration/catalog writes are performed.",
        ],
    }
    return result


def write_result(result: dict[str, Any]) -> Path:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = AUDIT_DIR / "01_l2_verification.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    return out_path


def main() -> int:
    result = verify_l2()
    out_path = write_result(result)
    total_check = next(check for check in result["checks"] if check["name"] == "L2 total row count")
    partition_check = next(check for check in result["checks"] if check["name"] == "16 ml_id partition coverage")
    print(f"Total L2 rows: {total_check['value']:,} ({total_check['status']})")
    print(f"Partitions: {partition_check['value']}/{partition_check['expected']} ({partition_check['status']})")
    for check in result["checks"]:
        print(f"  - {check['name']}: {check.get('status', 'INFO')}")
    print(f"Wrote {out_path.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
