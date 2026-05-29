#!/usr/bin/env python3
"""Build and load strategic ML JSON marts from general-view rows."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd

from brand_key_normalize import normalize_brand_name
from layer3_compute_general_v3 import (
    ALLOWED_SOURCES,
    GENERAL_BRAND_INSERT_COLUMNS,
    JSON_INSERT_COLUMNS,
    cagr_from_history,
    dumps,
    fill_periods,
    general_brand_jsonl_path,
    json_ready,
    mariadb_connect,
    mat_growth,
    pct_growth,
    read_jsonl,
    value_at,
    write_jsonl,
)
from layer3_compute_extended import compute_ei, compute_growth_contribution, compute_momentum
from layer3_compute_market_metric import compute_market_mart_payload
from layer3_normalize import prev_month, prev_quarter_month, same_month_prev_year
from ops_utils import configure_logging, find_project_root


LOGGER = configure_logging(__name__)
PROJECT_ROOT = find_project_root(Path(__file__).resolve())
CATALOG_DIR = PROJECT_ROOT / "output" / "catalog"
DRY_RUN_DIR = Path("/tmp")
ML_BRAND_JSONL = "strategic_ml_v3_brand_rows.jsonl"
ML_MARKET_JSONL = "strategic_ml_v3_market_rows.jsonl"
ML_BRAND_COLUMNS = [
    "ml_id",
    "brand_id",
    "brand_key",
    "brand_name",
    "source",
    "measure",
    "is_jw",
    "unit_label",
    "metric_history",
    "extended_metric_history",
    "channel_data",
    "specialty_data",
    "by_dimension",
    "raw_value_history",
    "overlay_data",
    "payload",
]
ML_MARKET_COLUMNS = [
    "ml_id",
    "ml_name",
    "source",
    "measure",
    "unit_label",
    "market_size_series",
    "hhi_series_5y",
    "brand_ranking_stacked",
    "company_ranking_stacked",
    "company_concentration_trend",
    "ei_ms_matrix",
    "growth_contribution_ms_matrix",
    "growth_contribution",
    "analysis_levels",
    "level_top5_trend",
    "target_customer_competition",
    "payload",
]
UBIST_MEASURES = ("sales", "volume")
IQVIA_MEASURES = ("sales", "unit", "dosage_unit", "counting_unit")


def _notna(value: Any) -> bool:
    try:
        return not bool(pd.isna(value))
    except Exception:
        return value is not None


def _truthy(value: Any) -> bool:
    if not _notna(value):
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "t", "yes", "y"}
    return bool(value)


def expected_measure_pairs(data_source: Any) -> set[tuple[str, str]]:
    value = str(data_source or "").strip().lower()
    expected: set[tuple[str, str]] = set()
    if value in {"ubist", "both", "dual"}:
        expected.update(("ubist", measure) for measure in UBIST_MEASURES)
    if value in {"iqvia", "iqvia_nsa", "both", "dual"}:
        expected.update(("iqvia_nsa", measure) for measure in IQVIA_MEASURES)
    if not expected:
        raise RuntimeError(f"Unsupported strategic data_source={data_source!r}")
    return expected


def load_catalogs() -> tuple[pd.DataFrame, pd.DataFrame]:
    ml_market = pd.read_parquet(CATALOG_DIR / "ml_market" / "ml_market.parquet")
    strategic_brand = pd.read_parquet(CATALOG_DIR / "strategic_brand" / "strategic_brand.parquet")
    if "general_brand_key" in strategic_brand.columns:
        strategic_brand["brand_key"] = strategic_brand["general_brand_key"].fillna(strategic_brand["name"]).map(normalize_brand_name)
    else:
        strategic_brand["brand_key"] = strategic_brand["name"].map(normalize_brand_name)
    return ml_market, strategic_brand


def fetch_general_rows_from_db(source: str | None = None) -> list[dict[str, Any]]:
    where = "WHERE source=%s" if source else ""
    params = (source,) if source else ()
    sql = "SELECT " + ",".join(GENERAL_BRAND_INSERT_COLUMNS) + " FROM mart_general_brand_metric " + where
    conn = mariadb_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    finally:
        conn.close()
    for row in rows:
        for col in GENERAL_BRAND_INSERT_COLUMNS:
            if col in {"metric_history", "extended_metric_history", "channel_data", "specialty_data", "by_dimension", "raw_value_history", "payload"}:
                row[col] = json.loads(row[col]) if row.get(col) else {}
        row["channel_specialty_matrix"] = {}
    return rows


def load_general_rows(output_dir: Path, source: str) -> list[dict[str, Any]]:
    rows = fetch_general_rows_from_db(source)
    if not rows:
        jsonl_rows = read_jsonl(general_brand_jsonl_path(source, output_dir))
        if jsonl_rows:
            raise RuntimeError(f"DB returned no {source} general rows while stale JSONL rows exist")
    return rows


def is_jw_name(name: Any) -> bool:
    text = str(name or "")
    return any(token in text for token in ("리바로", "가드", "라베칸", "제이클", "타발리스", "시그마트", "악템라", "페린젝트", "베노훼럼", "헴리브라", "엔커버", "위너프", "플라주오피"))


def catalog_by_key(brands: pd.DataFrame) -> dict[str, dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    brands = brands.copy()
    if "is_jw" not in brands.columns:
        brands["is_jw"] = False
    brands["_jw_sort"] = brands["is_jw"].map(_truthy).astype(int)
    brands = brands.sort_values(["_jw_sort", "brand_id"], ascending=[False, True])
    for key, part in brands.groupby("brand_key", dropna=False):
        if not key:
            continue
        first = part.iloc[0].to_dict()
        first["catalog_brand_ids"] = part["brand_id"].astype(str).tolist()
        first["catalog_names"] = part["name"].astype(str).tolist()
        grouped[str(key)] = first
    return grouped


def _display_brand_name(row: dict[str, Any], overlay: dict[str, Any]) -> str:
    if _truthy(overlay.get("is_jw")):
        canonical_name = overlay.get("canonical_name")
        if _notna(canonical_name) and str(canonical_name).strip():
            return str(canonical_name)
        return str(overlay.get("name") or row.get("brand_name") or row.get("brand_key") or "")
    return str(row.get("brand_name") or row.get("brand_key") or overlay.get("name") or "")


def _output_brand_key(row: dict[str, Any], overlay: dict[str, Any], display_name: str) -> str:
    if _truthy(overlay.get("is_jw")):
        return display_name
    return str(row.get("brand_key") or normalize_brand_name(display_name))


def validate_market_completeness(ml_row: pd.Series, catalog_rows: pd.DataFrame, selected: list[dict[str, Any]]) -> None:
    expected_pairs = expected_measure_pairs(ml_row.get("data_source"))
    actual_pairs = {(str(row.get("source")), str(row.get("measure"))) for row in selected}
    missing_market_pairs = expected_pairs - actual_pairs

    jw_catalog = catalog_rows.loc[catalog_rows.get("is_jw", False).map(_truthy)] if "is_jw" in catalog_rows.columns else pd.DataFrame()
    missing_jw: list[str] = []
    for _, catalog_row in jw_catalog.iterrows():
        join_key = str(catalog_row.get("brand_key") or "")
        display = str(catalog_row.get("canonical_name") or catalog_row.get("name") or join_key)
        present = {
            (str(row.get("source")), str(row.get("measure")))
            for row in selected
            if row.get("_catalog_join_key") == join_key
        }
        missing_pairs = expected_pairs - present
        if missing_pairs:
            missing_jw.append(f"{display}:{sorted(missing_pairs)}")

    if missing_market_pairs or missing_jw:
        raise RuntimeError(
            f"Strategic ML completeness failed for {ml_row.get('ml_id')} "
            f"market_missing={sorted(missing_market_pairs)} jw_missing={missing_jw}"
        )


def _row_raw_history(row: dict[str, Any], periods: list[str]) -> dict[str, float]:
    raw_history = row.get("raw_value_history") or {}
    metric_history = row.get("metric_history") or {}
    result: dict[str, float] = {}
    for period in periods:
        value = raw_history.get(period)
        if value is None and isinstance(metric_history.get(period), dict):
            value = metric_history[period].get("raw_value")
        try:
            result[period] = float(value or 0.0)
        except (TypeError, ValueError):
            result[period] = 0.0
    return result


def recompute_market_scoped_metric_history(rows: list[dict[str, Any]]) -> None:
    """Rewrite rank/MS fields at the selected strategic market scope.

    General mart rows are ATC4-scoped.  Strategic ML/CD marts select a narrower
    sibling set, so copying the general ``metric_history`` leaves stale rank and
    market share values.  This function keeps the brand raw histories and
    recalculates every period against the selected strategic rows.
    """

    periods = fill_periods(period for row in rows for period in (row.get("raw_value_history") or {}).keys())
    if not periods:
        periods = fill_periods(
            period
            for row in rows
            for period in (row.get("metric_history") or {}).keys()
        )
    if not periods:
        return

    raw_by_brand: dict[str, dict[str, float]] = {
        str(row.get("brand_name") or row.get("brand_key") or idx): _row_raw_history(row, periods)
        for idx, row in enumerate(rows)
    }
    market_history = {period: sum(history.get(period, 0.0) for history in raw_by_brand.values()) for period in periods}

    rank_by_period: dict[str, dict[str, int | None]] = {}
    for period in periods:
        ranked = sorted(
            ((brand, history.get(period, 0.0)) for brand, history in raw_by_brand.items() if history.get(period, 0.0) > 0),
            key=lambda item: (-item[1], item[0]),
        )
        rank_by_period[period] = {brand: idx + 1 for idx, (brand, _) in enumerate(ranked)}

    for idx, row in enumerate(rows):
        brand_name = str(row.get("brand_name") or row.get("brand_key") or idx)
        history = raw_by_brand[brand_name]
        metric_history = dict(row.get("metric_history") or {})
        extended_history = dict(row.get("extended_metric_history") or {})
        ms_values: list[float] = []

        for period in periods:
            value = history.get(period, 0.0)
            market_total = market_history.get(period, 0.0)
            ms_pct = (value / market_total * 100.0) if market_total > 0 else 0.0
            ms_values.append(ms_pct)

            prev = value_at(history, prev_month(period))
            prev_q = value_at(history, prev_quarter_month(period))
            prev_y = value_at(history, same_month_prev_year(period))
            market_prev_y = value_at(market_history, same_month_prev_year(period))
            growth_abs = value - prev_y if prev_y is not None else None
            market_growth_abs = market_history.get(period, 0.0) - market_prev_y if market_prev_y is not None else None
            growth_contribution, gc_warning = compute_growth_contribution(growth_abs, market_growth_abs)
            cagr_5y = cagr_from_history(history, period, 5)
            market_cagr_5y = cagr_from_history(market_history, period, 5)
            ei_5y, ei_warning = compute_ei(cagr_5y, market_cagr_5y)

            metric_payload = dict(metric_history.get(period) or {})
            metric_payload.update(
                {
                    "raw_value": value,
                    "ms": ms_pct,
                    "mom": pct_growth(value, prev),
                    "qoq": pct_growth(value, prev_q),
                    "yoy": pct_growth(value, prev_y),
                    "mat": mat_growth(history, period),
                    "growth_abs": growth_abs,
                    "rank": rank_by_period[period].get(brand_name) if value > 0 else None,
                }
            )
            metric_history[period] = metric_payload

            extended_payload = dict(extended_history.get(period) or {})
            extended_payload.update(
                {
                    "cagr_1y": cagr_from_history(history, period, 1),
                    "cagr_3y": cagr_from_history(history, period, 3),
                    "cagr_5y": cagr_5y,
                    "ei_5y": ei_5y,
                    "momentum_score": compute_momentum(ms_values[-4:]) if len(ms_values) >= 4 else None,
                    "growth_contribution": growth_contribution,
                    "growth_contribution_pct": growth_contribution,
                    "market_cagr_5y": market_cagr_5y,
                    "warnings": [warning for warning in (gc_warning, ei_warning) if warning],
                }
            )
            extended_history[period] = extended_payload

        row["raw_value_history"] = history
        row["metric_history"] = metric_history
        row["extended_metric_history"] = extended_history


def build_ml_rows(ml_row: pd.Series, catalog_rows: pd.DataFrame, general_rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_key = catalog_by_key(catalog_rows)
    expected_pairs = expected_measure_pairs(ml_row.get("data_source"))
    selected: list[dict[str, Any]] = []
    for row in general_rows:
        source_measure = (str(row.get("source")), str(row.get("measure")))
        if source_measure not in expected_pairs:
            continue
        overlay = by_key.get(str(row.get("brand_key")))
        if not overlay:
            continue
        copied = dict(row)
        display_name = _display_brand_name(copied, overlay)
        output_key = _output_brand_key(copied, overlay, display_name)
        dim = dict(copied.get("by_dimension") or {})
        for key in ("class", "class_1", "class_2", "molecule", "dosage_form", "strength_pack", "nhi_type", "ox_gx", "fish_oil"):
            dim[key] = overlay.get(key)
        copied.update(
            {
                "ml_id": ml_row["ml_id"],
                "brand_id": overlay.get("brand_id"),
                "brand_key": output_key,
                "brand_name": display_name,
                "is_jw": _truthy(overlay.get("is_jw")) if "is_jw" in overlay else is_jw_name(overlay.get("name")),
                "by_dimension": dim,
                "_catalog_join_key": str(overlay.get("brand_key") or row.get("brand_key") or ""),
                "overlay_data": {
                    "catalog_source": "strategic_brand",
                    "ml_id": ml_row["ml_id"],
                    "canonical_name": overlay.get("canonical_name"),
                    "general_brand_key": overlay.get("general_brand_key"),
                    "is_target": overlay.get("is_target"),
                    "catalog_brand_ids": overlay.get("catalog_brand_ids"),
                    "catalog_names": overlay.get("catalog_names"),
                    "class": overlay.get("class"),
                    "class_1": overlay.get("class_1"),
                    "class_2": overlay.get("class_2"),
                    "molecule": overlay.get("molecule"),
                    "dosage_form": overlay.get("dosage_form"),
                    "strength_pack": overlay.get("strength_pack"),
                    "nhi_type": overlay.get("nhi_type"),
                    "ox_gx": overlay.get("ox_gx"),
                    "fish_oil": overlay.get("fish_oil"),
                },
            }
        )
        selected.append(copied)

    validate_market_completeness(ml_row, catalog_rows, selected)
    for rows in _group_by_source_measure(selected).values():
        recompute_market_scoped_metric_history(rows)

    market_rows: list[dict[str, Any]] = []
    for (source, measure), rows in _group_by_source_measure(selected).items():
        payload = compute_market_mart_payload(rows, source=source, measure=measure, view_type="strategic_ml", catalog_market_row=ml_row.to_dict())
        market_rows.append(
            {
                "ml_id": ml_row["ml_id"],
                "ml_name": ml_row.get("name"),
                "source": source,
                "measure": measure,
                "unit_label": rows[0].get("unit_label") if rows else "",
                **payload,
            }
        )
    return selected, market_rows


def _group_by_source_measure(rows: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(str(row.get("source")), str(row.get("measure")))].append(row)
    return grouped


def insert_rows(table: str, columns: list[str], rows: list[dict[str, Any]], unique_cols: set[str], batch_size: int = 500) -> None:
    if not rows:
        return
    placeholders = ",".join(["%s"] * len(columns))
    update_sql = ",".join([f"{col}=VALUES({col})" for col in columns if col not in unique_cols])
    sql = f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders}) ON DUPLICATE KEY UPDATE {update_sql}"
    payloads = [
        tuple(dumps(row.get(col)) if col in JSON_INSERT_COLUMNS else row.get(col) for col in columns)
        for row in rows
    ]
    conn = mariadb_connect()
    try:
        with conn.cursor() as cur:
            for start in range(0, len(payloads), batch_size):
                cur.executemany(sql, payloads[start : start + batch_size])
    finally:
        conn.close()


def delete_existing_rows(table: str, market_col: str, market_ids: set[str]) -> None:
    if not market_ids:
        return
    placeholders = ",".join(["%s"] * len(market_ids))
    sql = f"DELETE FROM {table} WHERE {market_col} IN ({placeholders})"
    conn = mariadb_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(sorted(market_ids)))
    finally:
        conn.close()


def compute_strategic_ml(dry_run: bool, insert: bool, output_dir: Path, ml: str | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    if not dry_run and not insert:
        raise RuntimeError("Use --dry-run or --insert")
    ml_market, strategic_brand = load_catalogs()
    if ml:
        ml_market = ml_market.loc[ml_market["ml_id"] == ml]
    all_general: list[dict[str, Any]] = []
    for source in ALLOWED_SOURCES:
        all_general.extend(load_general_rows(output_dir, source))
    brand_rows: list[dict[str, Any]] = []
    market_rows: list[dict[str, Any]] = []
    for _, ml_row in ml_market.iterrows():
        catalog_rows = strategic_brand.loc[strategic_brand["ml_id"] == ml_row["ml_id"]].copy()
        rows, markets = build_ml_rows(ml_row, catalog_rows, all_general)
        brand_rows.extend(rows)
        market_rows.extend(markets)
    if dry_run:
        write_jsonl(output_dir / ML_BRAND_JSONL, brand_rows)
        write_jsonl(output_dir / ML_MARKET_JSONL, market_rows)
    if insert:
        market_ids = {str(row["ml_id"]) for _, row in ml_market.iterrows()}
        delete_existing_rows("mart_strategic_ml_brand_metric", "ml_id", market_ids)
        delete_existing_rows("mart_strategic_ml_market_metric", "ml_id", market_ids)
        insert_rows("mart_strategic_ml_brand_metric", ML_BRAND_COLUMNS, brand_rows, {"ml_id", "brand_id", "source", "measure"})
        insert_rows("mart_strategic_ml_market_metric", ML_MARKET_COLUMNS, market_rows, {"ml_id", "source", "measure"})
    stats = {"brand_rows": len(brand_rows), "market_rows": len(market_rows), "ml_count": int(ml_market["ml_id"].nunique())}
    return brand_rows, market_rows, stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ml")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--insert", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=DRY_RUN_DIR)
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    brand_rows, market_rows, stats = compute_strategic_ml(args.dry_run, args.insert, args.output_dir, ml=args.ml)
    print("\n=== strategic ML v3.1 ===")
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    if brand_rows:
        print("sample brand row:")
        print(json.dumps(json_ready(brand_rows[0]), ensure_ascii=False)[:1200])
    if market_rows:
        print("sample market row:")
        print(json.dumps(json_ready(market_rows[0]), ensure_ascii=False)[:1200])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
