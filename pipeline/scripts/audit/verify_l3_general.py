#!/usr/bin/env python3
"""Verify Layer 3 general marts with read-only SELECTs."""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api import db
from api.utils import loads_json_maybe
from ops_utils import find_project_root


PROJECT_ROOT = find_project_root(Path(__file__).resolve())
AUDIT_DIR = PROJECT_ROOT / "docs" / "audit" / "phase_16g4_side_verify_l3"
EXPECTED_GENERAL_BRAND_ROWS = 115_024
EXPECTED_GENERAL_MARKET_ROWS = 2_878
EXPECTED_UBIST_MEASURES = {"sales", "volume"}
EXPECTED_IQVIA_MEASURES = {"sales", "unit", "dosage_unit", "counting_unit"}
JW_BRANDS = ["리바로", "리바로젯", "시그마트", "가드메트", "페린젝트", "타발리스"]


def parse_json(value: Any, default: Any) -> Any:
    parsed = loads_json_maybe(value)
    return parsed if parsed is not None else default


def metric_raw_value(period_metric: Any) -> float | None:
    if isinstance(period_metric, dict):
        value = period_metric.get("raw_value", period_metric.get("value"))
    else:
        value = period_metric
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def pct(part: int | float, total: int | float) -> float:
    return round(float(part) / float(total) * 100, 4) if total else 0.0


def check_row_counts() -> dict[str, Any]:
    brand_count = int(db.fetch_one("SELECT COUNT(*) AS cnt FROM mart_general_brand_metric")["cnt"])
    market_count = int(db.fetch_one("SELECT COUNT(*) AS cnt FROM mart_general_market_metric")["cnt"])
    return {
        "name": "mart_general row counts",
        "brand_rows": brand_count,
        "market_rows": market_count,
        "expected_brand_rows": EXPECTED_GENERAL_BRAND_ROWS,
        "expected_market_rows": EXPECTED_GENERAL_MARKET_ROWS,
        "status": "PASS"
        if brand_count == EXPECTED_GENERAL_BRAND_ROWS and market_count == EXPECTED_GENERAL_MARKET_ROWS
        else "WARN",
    }


def check_brand_uniqueness() -> dict[str, Any]:
    row = db.fetch_one(
        """
        SELECT COUNT(*) AS total_rows,
               COUNT(DISTINCT brand_key) AS distinct_brand_keys,
               COUNT(DISTINCT CONCAT(brand_key, '|', atc4_code, '|', source, '|', measure)) AS distinct_grain
        FROM mart_general_brand_metric
        """
    )
    dupes = db.fetch_all(
        """
        SELECT brand_key, atc4_code, source, measure, COUNT(*) AS cnt
        FROM mart_general_brand_metric
        GROUP BY brand_key, atc4_code, source, measure
        HAVING cnt > 1
        ORDER BY cnt DESC
        LIMIT 20
        """
    )
    return {
        "name": "general brand grain uniqueness",
        "total_rows": int(row["total_rows"]),
        "distinct_brand_keys": int(row["distinct_brand_keys"]),
        "distinct_grain": int(row["distinct_grain"]),
        "duplicate_count_sampled": len(dupes),
        "duplicate_samples": dupes,
        "status": "PASS" if not dupes and int(row["total_rows"]) == int(row["distinct_grain"]) else "FAIL",
    }


def catalog_status_distribution() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = db.fetch_all(
        """
        SELECT
          source,
          COALESCE(JSON_UNQUOTE(JSON_EXTRACT(by_dimension, '$.catalog_status')), 'missing') AS catalog_status,
          COUNT(*) AS cnt
        FROM mart_general_brand_metric
        GROUP BY source, catalog_status
        ORDER BY source, catalog_status
        """
    )
    statuses = {row["catalog_status"] for row in rows}
    return rows, {
        "name": "catalog_status matched/unmatched distribution",
        "distribution": rows,
        "observed_statuses": sorted(statuses),
        "status": "PASS" if {"matched", "unmatched"}.issubset(statuses) else "WARN",
    }


def measure_distribution() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = db.fetch_all(
        """
        SELECT source, measure, COUNT(*) AS cnt, COUNT(DISTINCT brand_key) AS distinct_brand_keys,
               COUNT(DISTINCT atc4_code) AS distinct_atc4
        FROM mart_general_brand_metric
        GROUP BY source, measure
        ORDER BY source, measure
        """
    )
    observed = {
        source: {row["measure"] for row in rows if row["source"] == source}
        for source in {"ubist", "iqvia_nsa"}
    }
    ubist_missing = sorted(EXPECTED_UBIST_MEASURES - observed.get("ubist", set()))
    iqvia_missing = sorted(EXPECTED_IQVIA_MEASURES - observed.get("iqvia_nsa", set()))
    return rows, {
        "name": "measure coverage per source",
        "ubist_observed": sorted(observed.get("ubist", set())),
        "iqvia_observed": sorted(observed.get("iqvia_nsa", set())),
        "ubist_missing": ubist_missing,
        "iqvia_missing": iqvia_missing,
        "status": "PASS" if not ubist_missing and not iqvia_missing else "FAIL",
    }


def metric_distribution(sample_size: int = 200) -> dict[str, Any]:
    rows = db.fetch_all(
        """
        SELECT brand_key, source, measure, metric_history
        FROM mart_general_brand_metric
        ORDER BY id
        LIMIT %s
        """,
        [sample_size],
    )
    total = zero = null = negative = 0
    samples: list[dict[str, Any]] = []
    for row in rows:
        metric_history = parse_json(row["metric_history"], {})
        for period, metric in metric_history.items():
            value = metric_raw_value(metric)
            total += 1
            if value is None:
                null += 1
            elif value == 0:
                zero += 1
            elif value < 0:
                negative += 1
        if len(samples) < 5:
            samples.append(
                {
                    "brand_key": row["brand_key"],
                    "source": row["source"],
                    "measure": row["measure"],
                    "period_count": len(metric_history),
                }
            )
    return {
        "name": "metric_history zero/null/negative distribution",
        "sample_rows": len(rows),
        "total_periods": total,
        "zero_pct": pct(zero, total),
        "null_pct": pct(null, total),
        "negative_pct": pct(negative, total),
        "sample_period_counts": samples,
        "status": "PASS" if negative == 0 else "WARN",
    }


def raw_value_reconcile(sample_size: int = 50) -> dict[str, Any]:
    rows = db.fetch_all(
        """
        SELECT brand_key, atc4_code, source, measure, raw_value_history, metric_history
        FROM mart_general_brand_metric
        ORDER BY id
        LIMIT %s
        """,
        [sample_size],
    )
    mismatches: list[dict[str, Any]] = []
    checked = 0
    for row in rows:
        raw_history = parse_json(row["raw_value_history"], {})
        metric_history = parse_json(row["metric_history"], {})
        common_periods = sorted(set(raw_history) & set(metric_history))
        for period in common_periods[:10]:
            checked += 1
            raw_value = float(raw_history.get(period) or 0)
            metric_value = metric_raw_value(metric_history.get(period))
            if metric_value is None or abs(raw_value - metric_value) > 0.01:
                mismatches.append(
                    {
                        "brand_key": row["brand_key"],
                        "atc4_code": row["atc4_code"],
                        "source": row["source"],
                        "measure": row["measure"],
                        "period": period,
                        "raw_value": raw_value,
                        "metric_value": metric_value,
                    }
                )
    return {
        "name": "raw_value_history vs metric_history reconcile",
        "sample_rows": len(rows),
        "periods_checked": checked,
        "mismatch_count": len(mismatches),
        "mismatch_samples": mismatches[:10],
        "status": "PASS" if not mismatches else "WARN",
    }


def korean_dimension_keys(sample_size: int = 50) -> dict[str, Any]:
    rows = db.fetch_all(
        """
        SELECT channel_data, specialty_data
        FROM mart_general_brand_metric
        WHERE source = 'ubist'
        ORDER BY id
        LIMIT %s
        """,
        [sample_size],
    )
    channel_keys: Counter[str] = Counter()
    specialty_keys: Counter[str] = Counter()
    english_channel_codes = {"TH", "GH", "Semi", "CL"}
    for row in rows:
        channel_data = parse_json(row["channel_data"], {})
        specialty_data = parse_json(row["specialty_data"], {})
        channel_keys.update(str(key) for key in channel_data)
        specialty_keys.update(str(key) for key in specialty_data)
    code_leaks = sorted(key for key in channel_keys if key in english_channel_codes)
    return {
        "name": "channel_data/specialty_data Korean raw keys (S2)",
        "sample_rows": len(rows),
        "channel_key_samples": channel_keys.most_common(20),
        "specialty_key_samples": specialty_keys.most_common(20),
        "channel_code_leaks": code_leaks,
        "status": "PASS" if not code_leaks else "FAIL",
        "note": "CHSO and Specialty Unknown checks are intentionally out of scope per PL policy.",
    }


def products_distribution(sample_size: int = 100) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for source in ("ubist", "iqvia_nsa"):
        rows.extend(
            db.fetch_all(
                """
                SELECT source, by_dimension
                FROM mart_general_brand_metric
                WHERE source = %s
                ORDER BY id
                LIMIT %s
                """,
                [source, sample_size // 2],
            )
        )
    stats = {
        "ubist_total_products": 0,
        "ubist_product_id_null": 0,
        "iqvia_total_products": 0,
        "iqvia_product_id_filled": 0,
        "empty_products_rows": 0,
    }
    examples: list[dict[str, Any]] = []
    for row in rows:
        by_dimension = parse_json(row["by_dimension"], {})
        products = by_dimension.get("products") if isinstance(by_dimension, dict) else []
        if not products:
            stats["empty_products_rows"] += 1
            continue
        if len(examples) < 5:
            first = products[0]
            examples.append(
                {
                    "source": row["source"],
                    "product_count": len(products),
                    "first_product_keys": sorted(first) if isinstance(first, dict) else [],
                }
            )
        for product in products:
            product_id = product.get("product_id") or product.get("product_code") if isinstance(product, dict) else None
            if row["source"] == "ubist":
                stats["ubist_total_products"] += 1
                if not product_id or product.get("product_id") is None:
                    stats["ubist_product_id_null"] += 1
            elif row["source"] == "iqvia_nsa":
                stats["iqvia_total_products"] += 1
                if product_id:
                    stats["iqvia_product_id_filled"] += 1
    return {
        "name": "by_dimension.products[] structure and product_id distribution",
        **stats,
        "ubist_product_id_null_pct": pct(stats["ubist_product_id_null"], stats["ubist_total_products"]),
        "iqvia_product_id_filled_pct": pct(stats["iqvia_product_id_filled"], stats["iqvia_total_products"]),
        "examples": examples,
        "status": "PASS" if stats["empty_products_rows"] == 0 else "WARN",
    }


def jw_brand_samples() -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for brand in JW_BRANDS:
        rows = db.fetch_all(
            """
            SELECT brand_key, brand_name, atc4_code, source, measure, unit_label,
                   JSON_UNQUOTE(JSON_EXTRACT(by_dimension, '$.catalog_status')) AS catalog_status,
                   JSON_UNQUOTE(JSON_EXTRACT(by_dimension, '$.company')) AS company,
                   JSON_LENGTH(metric_history) AS period_count
            FROM mart_general_brand_metric
            WHERE brand_key = %s
            ORDER BY source, measure, atc4_code
            LIMIT 8
            """,
            [brand],
        )
        samples.extend(rows)
    return samples


def market_uniqueness() -> dict[str, Any]:
    dupes = db.fetch_all(
        """
        SELECT atc4_code, source, measure, COUNT(*) AS cnt
        FROM mart_general_market_metric
        GROUP BY atc4_code, source, measure
        HAVING cnt > 1
        ORDER BY cnt DESC
        LIMIT 20
        """
    )
    dist = db.fetch_all(
        """
        SELECT source, measure, COUNT(*) AS cnt
        FROM mart_general_market_metric
        GROUP BY source, measure
        ORDER BY source, measure
        """
    )
    return {
        "name": "mart_general_market_metric uniqueness and measure distribution",
        "duplicate_count_sampled": len(dupes),
        "duplicate_samples": dupes,
        "measure_distribution": dist,
        "status": "PASS" if not dupes else "FAIL",
    }


def verify_l3_general() -> dict[str, Any]:
    catalog_dist, catalog_check = catalog_status_distribution()
    measure_dist, measure_check = measure_distribution()
    checks = [
        check_row_counts(),
        check_brand_uniqueness(),
        catalog_check,
        measure_check,
        metric_distribution(),
        raw_value_reconcile(),
        korean_dimension_keys(),
        products_distribution(),
        market_uniqueness(),
    ]
    return {
        "phase": "16-G-4-Side-Verify-L3 (general)",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "checks": checks,
        "catalog_status_distribution": catalog_dist,
        "measure_distribution": measure_dist,
        "jw_brand_samples": jw_brand_samples(),
        "policy_notes": {
            "s2": "Raw Korean channel/specialty labels are expected; dictionary mapping is not required.",
            "chso": "CHSO is intentionally not verified because L3 mart does not include CHSO.",
            "specialty_unknown": "Specialty Unknown mapping checks are intentionally excluded per PL policy.",
            "direction_b": "General view is Layer 1 raw direct and bypasses Layer 2.",
        },
    }


def write_result(result: dict[str, Any]) -> Path:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    path = AUDIT_DIR / "01_general_verification.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def main() -> int:
    path = write_result(verify_l3_general())
    print(f"Done: {path.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
