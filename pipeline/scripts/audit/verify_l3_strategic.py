#!/usr/bin/env python3
"""Verify Layer 3 strategic ML/CD marts without mutating data."""

from __future__ import annotations

import json
import math
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "etl"))

import pandas as pd

from api import db
from api.utils import loads_json_maybe
from brand_key_normalize import normalize_brand_name
from ops_utils import find_project_root


PROJECT_ROOT = find_project_root(Path(__file__).resolve())
AUDIT_DIR = PROJECT_ROOT / "docs" / "audit" / "phase_16g4_side_verify_l3"
CATALOG_DIR = PROJECT_ROOT / "output" / "catalog"
OVERLAY_KEYS = ["class", "molecule", "dosage_form", "strength_pack", "nhi_type", "ox_gx", "fish_oil"]
JW_BRANDS = {"리바로", "리바로젯", "리바로브이", "리바로페노", "리바로하이", "페린젝트", "시그마트", "가드메트", "타발리스"}


def parse_json(value: Any, default: Any) -> Any:
    parsed = loads_json_maybe(value)
    return parsed if parsed is not None else default


def clean(value: Any) -> Any:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def load_catalog(name: str) -> pd.DataFrame:
    path = CATALOG_DIR / name / f"{name}.parquet"
    frame = pd.read_parquet(path)
    if "name" in frame.columns:
        frame["brand_key"] = frame["name"].map(normalize_brand_name)
    return frame


def catalog_row_map(frame: pd.DataFrame, key_cols: tuple[str, str]) -> dict[tuple[str, str], dict[str, Any]]:
    mapped: dict[tuple[str, str], dict[str, Any]] = {}
    for _, row in frame.iterrows():
        key = tuple(str(row[col]) for col in key_cols)
        mapped[key] = {col: clean(row[col]) for col in frame.columns}
    return mapped


def check_row_counts() -> dict[str, Any]:
    counts = {
        "ml_brand": int(db.fetch_one("SELECT COUNT(*) AS cnt FROM mart_strategic_ml_brand_metric")["cnt"]),
        "ml_market": int(db.fetch_one("SELECT COUNT(*) AS cnt FROM mart_strategic_ml_market_metric")["cnt"]),
        "cd_brand": int(db.fetch_one("SELECT COUNT(*) AS cnt FROM mart_strategic_cd_brand_metric")["cnt"]),
        "cd_market": int(db.fetch_one("SELECT COUNT(*) AS cnt FROM mart_strategic_cd_market_metric")["cnt"]),
    }
    expected = {"ml_brand": 16_438, "ml_market": 72, "cd_brand": 6_516, "cd_market": 88}
    return {
        "name": "mart_strategic row counts",
        **counts,
        "expected": expected,
        "status": "PASS" if counts == expected else "WARN",
    }


def check_ml_brand_filter(strategic_brand: pd.DataFrame) -> dict[str, Any]:
    catalog_ids = {(str(row["brand_id"]), str(row["ml_id"])) for _, row in strategic_brand.iterrows()}
    mart_rows = db.fetch_all(
        """
        SELECT DISTINCT brand_id, ml_id
        FROM mart_strategic_ml_brand_metric
        """
    )
    mart_ids = {(str(row["brand_id"]), str(row["ml_id"])) for row in mart_rows}
    mart_not_catalog = sorted(mart_ids - catalog_ids)[:20]
    catalog_not_mart = sorted(catalog_ids - mart_ids)
    catalog_by_id = catalog_row_map(strategic_brand, ("brand_id", "ml_id"))
    missing_samples = [
        {
            "brand_id": brand_id,
            "ml_id": ml_id,
            "name": catalog_by_id.get((brand_id, ml_id), {}).get("name"),
            "brand_key": catalog_by_id.get((brand_id, ml_id), {}).get("brand_key"),
        }
        for brand_id, ml_id in catalog_not_mart[:20]
    ]
    return {
        "name": "mart_strategic_ml brand filter consistency",
        "catalog_distinct_brand_ml": len(catalog_ids),
        "mart_distinct_brand_ml": len(mart_ids),
        "mart_not_in_catalog_count": len(mart_ids - catalog_ids),
        "mart_not_in_catalog_samples": mart_not_catalog,
        "catalog_not_in_mart_count": len(catalog_not_mart),
        "catalog_not_in_mart_samples": missing_samples,
        "status": "PASS" if not mart_not_catalog else "FAIL",
        "note": "C2 requires mart rows to be catalog matched. catalog_not_in_mart can be raw no-match/no-data.",
    }


def check_cd_brand_filter(cd_brand: pd.DataFrame) -> dict[str, Any]:
    catalog_ids = {(str(row["brand_id"]), str(row["cd_id"])) for _, row in cd_brand.iterrows()}
    mart_rows = db.fetch_all(
        """
        SELECT DISTINCT cd_brand_id, cd_market_id
        FROM mart_strategic_cd_brand_metric
        """
    )
    mart_ids = {(str(row["cd_brand_id"]), str(row["cd_market_id"])) for row in mart_rows}
    mart_not_catalog = sorted(mart_ids - catalog_ids)[:20]
    catalog_not_mart = sorted(catalog_ids - mart_ids)
    return {
        "name": "mart_strategic_cd brand filter consistency",
        "catalog_distinct_brand_cd": len(catalog_ids),
        "mart_distinct_brand_cd": len(mart_ids),
        "mart_not_in_catalog_count": len(mart_ids - catalog_ids),
        "mart_not_in_catalog_samples": mart_not_catalog,
        "catalog_not_in_mart_count": len(catalog_not_mart),
        "status": "PASS" if not mart_not_catalog else "FAIL",
    }


def overlay_check_for_table(
    table: str,
    id_col: str,
    brand_id_col: str,
    catalog: pd.DataFrame,
    catalog_id_col: str,
    limit: int = 500,
) -> dict[str, Any]:
    catalog_by_id = catalog_row_map(catalog, ("brand_id", catalog_id_col))
    rows = db.fetch_all(
        f"""
        SELECT {id_col} AS market_id, {brand_id_col} AS brand_id, brand_key, by_dimension, overlay_data
        FROM {table}
        ORDER BY id
        LIMIT %s
        """,
        [limit],
    )
    checked = 0
    mismatches: list[dict[str, Any]] = []
    null_catalog_raw_kept = 0
    samples: list[dict[str, Any]] = []
    for row in rows:
        catalog_row = catalog_by_id.get((str(row["brand_id"]), str(row["market_id"])), {})
        by_dimension = parse_json(row["by_dimension"], {})
        overlay_data = parse_json(row["overlay_data"], {})
        for key in OVERLAY_KEYS:
            catalog_value = clean(catalog_row.get(key))
            observed = clean(by_dimension.get(key)) if isinstance(by_dimension, dict) else None
            if catalog_value is not None:
                checked += 1
                if observed != catalog_value:
                    mismatches.append(
                        {
                            "market_id": row["market_id"],
                            "brand_id": row["brand_id"],
                            "brand_key": row["brand_key"],
                            "key": key,
                            "catalog": catalog_value,
                            "observed": observed,
                        }
                    )
            elif observed is not None:
                null_catalog_raw_kept += 1
        if len(samples) < 10:
            samples.append(
                {
                    "market_id": row["market_id"],
                    "brand_id": row["brand_id"],
                    "brand_key": row["brand_key"],
                    "by_dimension_overlay_keys": {key: by_dimension.get(key) for key in OVERLAY_KEYS if isinstance(by_dimension, dict) and by_dimension.get(key) is not None},
                    "overlay_source": overlay_data.get("catalog_source") if isinstance(overlay_data, dict) else None,
                }
            )
    return {
        "name": f"{table} overlay catalog priority",
        "sample_rows": len(rows),
        "non_null_catalog_values_checked": checked,
        "mismatch_count": len(mismatches),
        "mismatch_samples": mismatches[:20],
        "catalog_null_raw_value_kept_count": null_catalog_raw_kept,
        "samples": samples,
        "status": "PASS" if not mismatches else "WARN",
    }


def cd_overlay_check(limit: int = 500) -> dict[str, Any]:
    rows = db.fetch_all(
        """
        SELECT cd_market_id, cd_brand_id, brand_key, cd_overlay, overlay_data
        FROM mart_strategic_cd_brand_metric
        ORDER BY id
        LIMIT %s
        """,
        [limit],
    )
    missing_cd_overlay = 0
    missing_filter = 0
    missing_override = 0
    samples: list[dict[str, Any]] = []
    for row in rows:
        cd_overlay = parse_json(row["cd_overlay"], {})
        if not isinstance(cd_overlay, dict) or not cd_overlay:
            missing_cd_overlay += 1
            continue
        if "filter" not in cd_overlay:
            missing_filter += 1
        if "override_columns" not in cd_overlay:
            missing_override += 1
        if len(samples) < 10:
            samples.append(
                {
                    "cd_market_id": row["cd_market_id"],
                    "cd_brand_id": row["cd_brand_id"],
                    "brand_key": row["brand_key"],
                    "filter_keys": sorted((cd_overlay.get("filter") or {}).keys()) if isinstance(cd_overlay.get("filter"), dict) else [],
                    "override_keys": sorted((cd_overlay.get("override_columns") or {}).keys()) if isinstance(cd_overlay.get("override_columns"), dict) else [],
                    "additional_classes": cd_overlay.get("additional_classes"),
                }
            )
    return {
        "name": "mart_strategic_cd cd_overlay structure",
        "sample_rows": len(rows),
        "missing_cd_overlay": missing_cd_overlay,
        "missing_filter": missing_filter,
        "missing_override_columns": missing_override,
        "samples": samples,
        "status": "PASS" if not missing_cd_overlay and not missing_filter and not missing_override else "WARN",
    }


def hhi_range_check(table: str, id_col: str, limit: int = 200) -> dict[str, Any]:
    rows = db.fetch_all(
        f"""
        SELECT {id_col} AS market_id, source, measure, hhi_series_5y
        FROM {table}
        ORDER BY id
        LIMIT %s
        """,
        [limit],
    )
    checked = 0
    out_of_range: list[dict[str, Any]] = []
    for row in rows:
        hhi = parse_json(row["hhi_series_5y"], {})
        if not isinstance(hhi, dict):
            continue
        for period, value in hhi.items():
            if value is None:
                continue
            checked += 1
            val = float(value)
            if val < 0 or val > 10_000:
                out_of_range.append({**{k: row[k] for k in ("market_id", "source", "measure")}, "period": period, "value": val})
    return {
        "name": f"{table} HHI 0-10000 range",
        "market_rows_sampled": len(rows),
        "period_values_checked": checked,
        "out_of_range_count": len(out_of_range),
        "out_of_range_samples": out_of_range[:20],
        "status": "PASS" if not out_of_range else "FAIL",
    }


def brand_ranking_ms_check(table: str, id_col: str, limit: int = 50) -> dict[str, Any]:
    rows = db.fetch_all(
        f"""
        SELECT {id_col} AS market_id, source, measure, brand_ranking_stacked
        FROM {table}
        ORDER BY id
        LIMIT %s
        """,
        [limit],
    )
    period_samples: list[dict[str, Any]] = []
    max_ms_total = 0.0
    for row in rows:
        ranking = parse_json(row["brand_ranking_stacked"], {})
        if not isinstance(ranking, dict) or not ranking:
            continue
        period = sorted(ranking)[-1]
        brands = ranking.get(period)
        if not isinstance(brands, list):
            continue
        ms_total = sum(float(item.get("ms") or 0) for item in brands if isinstance(item, dict))
        max_ms_total = max(max_ms_total, ms_total)
        if len(period_samples) < 20:
            period_samples.append(
                {
                    "market_id": row["market_id"],
                    "source": row["source"],
                    "measure": row["measure"],
                    "period": period,
                    "brand_count": len(brands),
                    "ms_sum_topn": round(ms_total, 4),
                }
            )
    return {
        "name": f"{table} brand_ranking ms sum sanity",
        "samples": period_samples,
        "max_topn_ms_sum": round(max_ms_total, 4),
        "status": "PASS" if max_ms_total <= 100.0001 else "WARN",
        "note": "Ranking payload is top-N, so ms_sum_topn can be below 100 and is checked as <= 100.",
    }


def market_size_reconcile(table_brand: str, table_market: str, id_col: str, history_col: str = "raw_value_history", limit: int = 12) -> dict[str, Any]:
    market_rows = db.fetch_all(
        f"""
        SELECT {id_col} AS market_id, source, measure, market_size_series
        FROM {table_market}
        ORDER BY id
        LIMIT %s
        """,
        [limit],
    )
    mismatches: list[dict[str, Any]] = []
    checked = 0
    samples: list[dict[str, Any]] = []
    for market in market_rows:
        brand_rows = db.fetch_all(
            f"""
            SELECT {history_col}
            FROM {table_brand}
            WHERE {id_col} = %s AND source = %s AND measure = %s
            """,
            [market["market_id"], market["source"], market["measure"]],
        )
        summed: Counter[str] = Counter()
        for brand in brand_rows:
            history = parse_json(brand[history_col], {})
            if isinstance(history, dict):
                for period, value in history.items():
                    summed[str(period)] += float(value or 0)
        market_series = parse_json(market["market_size_series"], {})
        if not isinstance(market_series, dict):
            continue
        for period in sorted(set(market_series) & set(summed))[:10]:
            checked += 1
            market_value = float(market_series.get(period) or 0)
            brand_sum = float(summed.get(period) or 0)
            if abs(market_value - brand_sum) > max(0.01, abs(market_value) * 0.000001):
                mismatches.append(
                    {
                        "market_id": market["market_id"],
                        "source": market["source"],
                        "measure": market["measure"],
                        "period": period,
                        "market_value": market_value,
                        "brand_sum": brand_sum,
                    }
                )
        if len(samples) < 10:
            samples.append(
                {
                    "market_id": market["market_id"],
                    "source": market["source"],
                    "measure": market["measure"],
                    "brand_rows": len(brand_rows),
                    "period_count": len(market_series),
                }
            )
    return {
        "name": f"{table_market} market_size_series vs brand raw sum",
        "market_rows_sampled": len(market_rows),
        "periods_checked": checked,
        "mismatch_count": len(mismatches),
        "mismatch_samples": mismatches[:20],
        "samples": samples,
        "status": "PASS" if not mismatches else "WARN",
    }


def analysis_levels_check(table: str, id_col: str, catalog: pd.DataFrame, catalog_id_col: str, limit: int = 200) -> dict[str, Any]:
    catalog_flags = {
        str(row[catalog_id_col]): {key.replace("analyze_", ""): bool(clean(row[key])) for key in catalog.columns if key.startswith("analyze_")}
        for _, row in catalog.iterrows()
    }
    rows = db.fetch_all(
        f"""
        SELECT {id_col} AS market_id, source, measure, analysis_levels
        FROM {table}
        ORDER BY id
        LIMIT %s
        """,
        [limit],
    )
    samples: list[dict[str, Any]] = []
    no_payload = 0
    for row in rows:
        levels = parse_json(row["analysis_levels"], None)
        if not levels:
            no_payload += 1
            continue
        keys = set(levels.keys()) if isinstance(levels, dict) else set()
        expected_true = {key for key, enabled in catalog_flags.get(str(row["market_id"]), {}).items() if enabled and key != "target_customer"}
        if len(samples) < 20:
            samples.append(
                {
                    "market_id": row["market_id"],
                    "source": row["source"],
                    "measure": row["measure"],
                    "analysis_keys": sorted(keys),
                    "catalog_true_flags": sorted(expected_true),
                    "intersection": sorted(keys & expected_true),
                }
            )
    return {
        "name": f"{table} analysis_levels vs catalog analyze_* flags",
        "rows_sampled": len(rows),
        "empty_analysis_levels": no_payload,
        "samples": samples,
        "status": "PASS" if samples else "WARN",
    }


def is_jw_check(table: str) -> dict[str, Any]:
    dist = db.fetch_all(f"SELECT is_jw, COUNT(*) AS cnt FROM {table} GROUP BY is_jw ORDER BY is_jw")
    rows = db.fetch_all(
        f"""
        SELECT brand_key, is_jw, COUNT(*) AS cnt
        FROM {table}
        WHERE brand_key IN ({','.join(['%s'] * len(JW_BRANDS))})
        GROUP BY brand_key, is_jw
        ORDER BY brand_key, is_jw
        """,
        sorted(JW_BRANDS),
    )
    false_jw = [row for row in rows if row["brand_key"] in JW_BRANDS and int(row["is_jw"]) != 1]
    return {
        "name": f"{table} is_jw flag sanity",
        "distribution": dist,
        "jw_brand_samples": rows,
        "jw_false_samples": false_jw,
        "status": "PASS" if not false_jw else "WARN",
    }


def market_row_structure() -> dict[str, Any]:
    ml_dist = db.fetch_all(
        """
        SELECT source, measure, COUNT(*) AS cnt
        FROM mart_strategic_ml_market_metric
        GROUP BY source, measure
        ORDER BY source, measure
        """
    )
    ml_per_market = db.fetch_all(
        """
        SELECT ml_id, COUNT(*) AS row_count
        FROM mart_strategic_ml_market_metric
        GROUP BY ml_id
        ORDER BY ml_id
        """
    )
    cd_dist = db.fetch_all(
        """
        SELECT source, measure, COUNT(*) AS cnt
        FROM mart_strategic_cd_market_metric
        GROUP BY source, measure
        ORDER BY source, measure
        """
    )
    cd_per_market = db.fetch_all(
        """
        SELECT cd_market_id, COUNT(*) AS row_count
        FROM mart_strategic_cd_market_metric
        GROUP BY cd_market_id
        ORDER BY cd_market_id
        """
    )
    return {
        "name": "strategic market row structure",
        "ml_by_source_measure": ml_dist,
        "ml_by_market": ml_per_market,
        "cd_by_source_measure": cd_dist,
        "cd_by_market": cd_per_market,
        "ml_total": sum(int(row["cnt"]) for row in ml_dist),
        "cd_total": sum(int(row["cnt"]) for row in cd_dist),
        "status": "PASS"
        if sum(int(row["cnt"]) for row in ml_dist) == 72 and sum(int(row["cnt"]) for row in cd_dist) == 88
        else "WARN",
    }


def verify_l3_strategic() -> dict[str, Any]:
    ml_market = load_catalog("ml_market")
    cd_market = load_catalog("cd_market")
    strategic_brand = load_catalog("strategic_brand")
    cd_brand = load_catalog("cd_brand")
    checks = [
        check_row_counts(),
        check_ml_brand_filter(strategic_brand),
        check_cd_brand_filter(cd_brand),
        overlay_check_for_table("mart_strategic_ml_brand_metric", "ml_id", "brand_id", strategic_brand, "ml_id"),
        overlay_check_for_table("mart_strategic_cd_brand_metric", "cd_market_id", "cd_brand_id", cd_brand, "cd_id"),
        cd_overlay_check(),
        hhi_range_check("mart_strategic_ml_market_metric", "ml_id"),
        hhi_range_check("mart_strategic_cd_market_metric", "cd_market_id"),
        brand_ranking_ms_check("mart_strategic_ml_market_metric", "ml_id"),
        brand_ranking_ms_check("mart_strategic_cd_market_metric", "cd_market_id"),
        market_size_reconcile("mart_strategic_ml_brand_metric", "mart_strategic_ml_market_metric", "ml_id"),
        market_size_reconcile("mart_strategic_cd_brand_metric", "mart_strategic_cd_market_metric", "cd_market_id"),
        analysis_levels_check("mart_strategic_ml_market_metric", "ml_id", ml_market, "ml_id"),
        analysis_levels_check("mart_strategic_cd_market_metric", "cd_market_id", cd_market, "cd_id"),
        is_jw_check("mart_strategic_ml_brand_metric"),
        is_jw_check("mart_strategic_cd_brand_metric"),
        market_row_structure(),
    ]
    return {
        "phase": "16-G-4-Side-Verify-L3 (strategic)",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "checks": checks,
        "catalog_inventory": {
            "ml_market": len(ml_market),
            "cd_market": len(cd_market),
            "strategic_brand": len(strategic_brand),
            "cd_brand": len(cd_brand),
        },
    }


def write_result(result: dict[str, Any]) -> Path:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    path = AUDIT_DIR / "02_strategic_verification.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return path


def main() -> int:
    path = write_result(verify_l3_strategic())
    print(f"Done: {path.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
