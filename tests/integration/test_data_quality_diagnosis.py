"""Phase A-2-2-Side3 data-quality diagnosis collector.

This is intentionally read-only against the running local demo API and local
MariaDB. It writes diagnostic evidence under /tmp/jw_diagnosis and, when run as
a script, produces the phase audit directory plus zip.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

try:
    import pandas as pd
except Exception:  # pragma: no cover - diagnostics still emit partial evidence
    pd = None

try:
    import pymysql
except Exception:  # pragma: no cover
    pymysql = None


REPO_ROOT = Path(__file__).resolve().parents[2]
API_BASE = os.getenv("LOCAL_API_BASE", "http://localhost:8000").rstrip("/")
OUT_DIR = Path(os.getenv("JW_DIAGNOSIS_OUT", "/tmp/jw_diagnosis"))
LIVE_ENV_PATH = Path("/tmp/jw_live_demo/backend_env.sh")

VIEWS = ["general", "strategic_ml", "strategic_cd"]
SOURCES = ["ubist", "iqvia"]
SOURCE_MEASURES = {
    "ubist": ["sales", "volume"],
    "iqvia": ["sales", "unit", "dosage_unit", "counting_unit"],
}

PREFERRED_BOOT_BRANDS = [
    "리바로",
    "리바로젯2",
    "리바로젯4",
    "리바로브이",
    "리바로하이",
    "리바로페노2",
    "가드메트",
    "페린젝트",
    "시그마트",
    "타발리스",
    "엔커버",
    "위너프",
    "라베칸",
    "라베칸듀오",
    "제이클",
    "악템라",
    "베노훼럼",
    "플라주오피",
]

JW_NAME_RE = re.compile(
    "리바로|가드메트|페린젝트|시그마트|타발리스|라베칸|엔커버|위너프|제이클|악템라|베노훼럼|플라주오피|헴리브라|나도가드|라베가드|에소가드|자이가드"
)


def now_tag() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M")


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def fetch_json(path: str, params: dict[str, Any] | None = None, timeout: float = 30.0) -> tuple[int, Any]:
    query = urllib.parse.urlencode(params or {})
    url = f"{API_BASE}{path}"
    if query:
        url = f"{url}?{query}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw.decode("utf-8"))
            except Exception:
                return resp.status, raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            body: Any = json.loads(raw.decode("utf-8"))
        except Exception:
            body = raw.decode("utf-8", errors="replace")
        return exc.code, body


def normalize_source(source: str | None) -> str | None:
    if not source:
        return source
    lowered = source.lower()
    if "iqvia" in lowered:
        return "iqvia"
    if "ubist" in lowered:
        return "ubist"
    return lowered


def summarize_value(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        keys = list(value.keys())
        return {"type": "dict", "len": len(value), "keys": keys[:12]}
    if isinstance(value, list):
        first_keys = list(value[0].keys())[:12] if value and isinstance(value[0], dict) else None
        return {"type": "list", "len": len(value), "first_keys": first_keys}
    return {"type": type(value).__name__, "repr": str(value)[:200]}


def get_path(obj: Any, dotted_path: str) -> Any:
    current = obj
    for part in dotted_path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def path_len(obj: Any, dotted_path: str) -> int:
    value = get_path(obj, dotted_path)
    if isinstance(value, (dict, list)):
        return len(value)
    return 0


def series_rows(value: Any) -> int:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        if isinstance(value.get("data"), list):
            return len(value["data"])
        return len(value)
    return 0


def nonempty_periods(value: Any) -> int:
    if not isinstance(value, dict):
        return 0
    return sum(1 for item in value.values() if isinstance(item, (list, dict)) and len(item) > 0)


def compact_brand(brand: dict[str, Any]) -> dict[str, Any]:
    return {
        "brand_key": brand.get("brand_key"),
        "brand_name": brand.get("brand_name"),
        "is_jw": brand.get("is_jw"),
        "catalog_status": brand.get("catalog_status"),
        "available_sources": brand.get("available_sources", []),
        "available_sources_normalized": sorted(
            {normalize_source(src) for src in brand.get("available_sources", []) if normalize_source(src)}
        ),
        "available_views": brand.get("available_views", []),
        "available_measures": brand.get("available_measures", []),
        "market_ids": brand.get("market_ids", []),
        "source_marts": brand.get("source_marts", []),
        "company": brand.get("company"),
    }


def selected_mockup_brands(api_brands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    for item in api_brands:
        key = item.get("brand_key") or item.get("brand_name")
        if key and key not in by_key:
            by_key[key] = item

    selected = [by_key[name] for name in PREFERRED_BOOT_BRANDS if name in by_key]
    if len(selected) < 12:
        selected_keys = {(item.get("brand_key") or item.get("brand_name")) for item in selected}
        for item in api_brands[:40]:
            key = item.get("brand_key") or item.get("brand_name")
            if key and key not in selected_keys:
                selected.append(item)
                selected_keys.add(key)
            if len(selected) >= 24:
                break
    return selected[:24]


def load_live_demo_env() -> dict[str, str]:
    values = {}
    if LIVE_ENV_PATH.exists():
        for line in LIVE_ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line.startswith("export "):
                continue
            key, _, value = line[len("export ") :].partition("=")
            values[key.strip()] = value.strip().strip("'\"")
    for key in ["DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DB_NAME"]:
        if os.getenv(key):
            values[key] = os.getenv(key, "")
    return values


def db_connect():
    if pymysql is None:
        raise RuntimeError("pymysql is not installed")
    env = load_live_demo_env()
    return pymysql.connect(
        host=env.get("DB_HOST", "127.0.0.1"),
        port=int(env.get("DB_PORT", "3308")),
        user=env.get("DB_USER", "root"),
        password=env.get("DB_PASSWORD", ""),
        database=env.get("DB_NAME", "jw_mart"),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )


def collect_preflight() -> dict[str, Any]:
    status, health = fetch_json("/api/health", timeout=10.0)
    result: dict[str, Any] = {"api_health_status": status, "api_health_body": health}
    try:
        backend_pid = Path("/tmp/jw_live_demo/backend.pid").read_text().strip()
        proxy_pid = Path("/tmp/jw_live_demo/proxy.pid").read_text().strip()
        result["backend_pid"] = backend_pid
        result["proxy_pid"] = proxy_pid
        result["backend_running"] = subprocess.run(["ps", "-p", backend_pid], capture_output=True).returncode == 0
        result["proxy_running"] = subprocess.run(["ps", "-p", proxy_pid], capture_output=True).returncode == 0
    except Exception as exc:
        result["pid_check_error"] = str(exc)
    write_json(OUT_DIR / "00_preflight.json", result)
    return result


def collect_db_checks() -> dict[str, Any]:
    result: dict[str, Any] = {"available": False}
    if pymysql is None:
        result["error"] = "pymysql unavailable"
        write_json(OUT_DIR / "00_db_check.json", result)
        return result

    queries = {
        "table_counts": """
            SELECT 'cache_brands' AS table_name, COUNT(*) AS row_count FROM cache_brands
            UNION ALL SELECT 'cache_market_status', COUNT(*) FROM cache_market_status
            UNION ALL SELECT 'cache_cause', COUNT(*) FROM cache_cause
            UNION ALL SELECT 'cache_deep_analysis', COUNT(*) FROM cache_deep_analysis
        """,
        "cache_brands_breakdown": """
            SELECT view_type, source, JSON_LENGTH(response_json, '$.brands') AS brand_count,
                   payload_size, updated_at
            FROM cache_brands
            ORDER BY view_type, source
        """,
        "cache_cause_breakdown": """
            SELECT view_type, source, measure, COUNT(*) AS row_count,
                   COUNT(DISTINCT brand_key) AS brand_count,
                   COUNT(DISTINCT market_id) AS market_count
            FROM cache_cause
            GROUP BY view_type, source, measure
            ORDER BY view_type, source, measure
        """,
        "cache_deep_breakdown": """
            SELECT view_type, source, measure, COUNT(*) AS row_count,
                   COUNT(DISTINCT brand_key) AS brand_count,
                   COUNT(DISTINCT market_id) AS market_count
            FROM cache_deep_analysis
            GROUP BY view_type, source, measure
            ORDER BY view_type, source, measure
        """,
        "cache_market_status_breakdown": """
            SELECT view_type, source, measure, COUNT(*) AS row_count,
                   COUNT(DISTINCT market_id) AS market_count
            FROM cache_market_status
            GROUP BY view_type, source, measure
            ORDER BY view_type, source, measure
        """,
        "jw_cache_cause_brand_distribution": """
            SELECT brand_key, MAX(brand_name) AS brand_name, COUNT(*) AS row_count,
                   GROUP_CONCAT(DISTINCT source ORDER BY source) AS sources,
                   GROUP_CONCAT(DISTINCT view_type ORDER BY view_type) AS views,
                   GROUP_CONCAT(DISTINCT measure ORDER BY measure) AS measures,
                   GROUP_CONCAT(DISTINCT market_id ORDER BY market_id SEPARATOR ',') AS market_ids
            FROM cache_cause
            WHERE is_jw = 1
            GROUP BY brand_key
            ORDER BY brand_key
        """,
        "market_status_rows": """
            SELECT view_type, market_id, source, measure, market_name
            FROM cache_market_status
            ORDER BY view_type, market_id, source, measure
        """,
    }
    try:
        with db_connect() as conn:
            result["available"] = True
            with conn.cursor() as cur:
                for name, sql in queries.items():
                    cur.execute(sql)
                    result[name] = list(cur.fetchall())
    except Exception as exc:
        result["error"] = str(exc)
    write_json(OUT_DIR / "00_db_check.json", result)
    return result


def collect_brands() -> dict[str, Any]:
    brand_dir = OUT_DIR / "brands"
    brand_dir.mkdir(parents=True, exist_ok=True)

    status, default_body = fetch_json("/api/brands")
    write_json(brand_dir / "brands_default.json", {"status": status, "body": default_body})
    default_brands = default_body.get("brands", []) if isinstance(default_body, dict) else []

    combos: dict[str, Any] = {}
    for view in VIEWS:
        for source in SOURCES:
            status, body = fetch_json("/api/brands", {"view": view, "source": source})
            path = brand_dir / f"brands_{view}_{source}.json"
            write_json(path, {"status": status, "body": body})
            brands = body.get("brands", []) if isinstance(body, dict) else []
            combos[f"{view}/{source}"] = {
                "status": status,
                "total": len(brands),
                "jw": sum(1 for item in brands if item.get("is_jw")),
                "source_distribution": {
                    str(key): count
                    for key, count in Counter(
                        tuple(sorted(item.get("available_sources", []))) for item in brands if item.get("is_jw")
                    ).items()
                },
            }

    jw_brands = [compact_brand(item) for item in default_brands if item.get("is_jw")]
    selected = [compact_brand(item) for item in selected_mockup_brands(default_brands)]
    source_distribution = Counter(tuple(item["available_sources_normalized"]) for item in jw_brands)
    selected_source_distribution = Counter(tuple(item["available_sources_normalized"]) for item in selected)

    result = {
        "default_status": status,
        "default_total": len(default_brands),
        "default_jw_total": len(jw_brands),
        "default_non_jw_total": len(default_brands) - len(jw_brands),
        "combos": combos,
        "jw_brand_list": jw_brands,
        "jw_source_distribution": {str(key): value for key, value in source_distribution.items()},
        "selected_mockup_brand_count": len(selected),
        "selected_mockup_brands": selected,
        "selected_source_distribution": {str(key): value for key, value in selected_source_distribution.items()},
        "preferred_boot_brands": PREFERRED_BOOT_BRANDS,
        "preferred_missing_in_api": [name for name in PREFERRED_BOOT_BRANDS if name not in {b["brand_key"] for b in jw_brands}],
        "api_jw_not_in_preferred": [
            item["brand_key"] for item in jw_brands if item["brand_key"] not in set(PREFERRED_BOOT_BRANDS)
        ],
    }
    write_json(OUT_DIR / "01_brand_inventory.json", result)
    write_json(OUT_DIR / "brand_selection_analysis.json", result)
    return result


def safe_records(df: Any, columns: list[str] | None = None, limit: int = 200) -> list[dict[str, Any]]:
    if df is None:
        return []
    if columns:
        columns = [col for col in columns if col in df.columns]
        df = df[columns]
    return df.head(limit).where(pd.notnull(df), None).to_dict(orient="records")


def collect_catalog() -> dict[str, Any]:
    result: dict[str, Any] = {"available": pd is not None}
    if pd is None:
        result["error"] = "pandas unavailable"
        write_json(OUT_DIR / "05_catalog_vs_cache_mapping.json", result)
        return result

    catalog_root = REPO_ROOT / "output" / "catalog"
    tables = [
        "ml_market",
        "cd_market",
        "cd_filter",
        "strategic_brand",
        "strategic_product",
        "cd_brand",
        "cd_product",
    ]
    result["tables"] = {}
    frames: dict[str, Any] = {}
    for table in tables:
        path = catalog_root / table / f"{table}.parquet"
        if not path.exists():
            result["tables"][table] = {"exists": False}
            continue
        df = pd.read_parquet(path)
        frames[table] = df
        result["tables"][table] = {
            "exists": True,
            "rows": len(df),
            "columns": list(df.columns),
            "sample": safe_records(df, limit=3),
        }

    sb = frames.get("strategic_brand")
    sp = frames.get("strategic_product")
    if sb is not None:
        text_cols = [col for col in ["name", "merge_name", "brand_id", "판매사", "제조사", "ml_id", "cd_id"] if col in sb.columns]
        mask = False
        for col in [c for c in ["name", "merge_name", "판매사", "제조사"] if c in sb.columns]:
            mask = mask | sb[col].astype(str).str.contains(JW_NAME_RE, na=False)
        jw_like = sb[mask] if not isinstance(mask, bool) else sb.iloc[0:0]
        result["strategic_brand_jw_like_count"] = len(jw_like)
        result["strategic_brand_jw_like"] = safe_records(jw_like, text_cols + ["class", "molecule", "dosage_form", "strength_pack"], 100)
        if "merge_name" in sb.columns:
            result["strategic_brand_merge_name_counts"] = (
                jw_like.groupby("merge_name").size().sort_values(ascending=False).head(80).to_dict()
            )
        if "name" in sb.columns:
            result["dose_split_rows"] = safe_records(
                sb[
                    sb["name"].astype(str).str.contains("리바로젯|리바로페노|라베칸", na=False)
                    | sb.get("merge_name", sb["name"]).astype(str).str.contains("리바로젯|리바로페노|라베칸", na=False)
                ],
                text_cols + ["class", "molecule", "dosage_form", "strength_pack"],
                200,
            )

    if sp is not None and sb is not None and "brand_id" in sp.columns and "brand_id" in sb.columns:
        brand_cols = [col for col in ["brand_id", "name", "merge_name", "ml_id", "cd_id"] if col in sb.columns]
        merged = sp.merge(sb[brand_cols], on="brand_id", how="left", suffixes=("_product", "_brand"))
        name_candidates = [
            "merge_name_brand",
            "merge_name",
            "name_brand",
            "name",
            "merge_name_product",
            "name_product",
        ]
        name_col = next((col for col in name_candidates if col in merged.columns), None)
        if name_col is None:
            result["strategic_product_join_columns"] = list(merged.columns)
            mask = pd.Series([False] * len(merged), index=merged.index)
        else:
            mask = merged[name_col].astype(str).str.contains(JW_NAME_RE, na=False)
        jw_sp = merged[mask]
        result["strategic_product_jw_like_count"] = len(jw_sp)
        if name_col and name_col in jw_sp.columns:
            result["strategic_product_by_catalog_brand"] = (
                jw_sp.groupby(name_col).size().sort_values(ascending=False).head(100).to_dict()
            )

    write_json(OUT_DIR / "05_catalog_vs_cache_mapping.json", result)
    return result


def collect_mockup_static() -> dict[str, Any]:
    path = REPO_ROOT / "docs" / "reference" / "jw_market_hardcoded_mockup_v2.html"
    html = path.read_text(encoding="utf-8")
    lines = html.splitlines()

    def grep(pattern: str, before: int = 2, after: int = 8, limit: int = 12) -> list[dict[str, Any]]:
        regex = re.compile(pattern)
        hits = []
        for idx, line in enumerate(lines, start=1):
            if regex.search(line):
                start = max(1, idx - before)
                end = min(len(lines), idx + after)
                hits.append(
                    {
                        "line": idx,
                        "match": line.strip(),
                        "snippet": "\n".join(f"{n}: {lines[n - 1]}" for n in range(start, end + 1)),
                    }
                )
                if len(hits) >= limit:
                    break
        return hits

    preferred_match = re.search(r"const\s+PREFERRED_BOOT_BRANDS\s*=\s*\[(.*?)\];", html, re.S)
    preferred_in_file = []
    if preferred_match:
        preferred_in_file = re.findall(r"'([^']+)'", preferred_match.group(1))

    result = {
        "file": str(path),
        "line_count": len(lines),
        "api_base": grep(r"const API_BASE", 0, 1),
        "hardcoded_brand_count_text": grep(r"브랜드 25개|JW 25개|brand-count", 2, 4, 20),
        "preferred_boot_brands": preferred_in_file,
        "preferred_boot_brand_count": len(preferred_in_file),
        "select_preferred_function": grep(r"function selectPreferredApiBrands", 0, 42, 1),
        "normalize_api_brand_item": grep(r"function normalizeApiBrandItem", 0, 55, 1),
        "build_status_card": grep(r"function buildStatusCard", 0, 60, 1),
        "source_toggle_logic": grep(r"source-toggle|is_dual_source", 3, 8, 20),
        "company_hhi_renderer": grep(r"function renderCompanyHHI|company_concentration_trend", 2, 36, 10),
        "cause_unwrap_logic": grep(r"function unwrapNewCauseResponse", 0, 90, 1),
        "forecast_renderer": grep(r"function renderForecastChartFromData", 0, 70, 1),
        "empty_chart_context": grep(r"A\\.5|D\\.1|D\\.2|Waterfall|Top5|Matrix", 2, 8, 20),
    }
    write_json(OUT_DIR / "06_mockup_js_analysis.json", result)
    return result


def summarize_cause_response(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        return {"body_type": type(body).__name__}
    data = body.get("data", {})
    sources_data = data.get("sources_data", {}) if isinstance(data, dict) else {}
    company_conc = data.get("company_concentration_trend") if isinstance(data, dict) else None
    company_ranking = data.get("company_ranking_stacked") if isinstance(data, dict) else None
    level_top5 = data.get("level_top5_trend") if isinstance(data, dict) else None
    ei_ms = data.get("ei_ms_matrix") if isinstance(data, dict) else None
    growth = data.get("growth_contribution") if isinstance(data, dict) else None
    growth_ms = data.get("growth_contribution_ms_matrix") if isinstance(data, dict) else None

    return {
        "top_keys": list(body.keys()),
        "data_keys": list(data.keys()) if isinstance(data, dict) else [],
        "brand_key": body.get("brand_key"),
        "market_id": body.get("market_id"),
        "market_cache_key": body.get("market_cache_key"),
        "view": body.get("view"),
        "source": body.get("source"),
        "measure": body.get("measure"),
        "kpi_keys": list(data.get("kpi", {}).keys()) if isinstance(data.get("kpi"), dict) else [],
        "metric_history_periods": len(sources_data.get("metric_history", {})) if isinstance(sources_data, dict) else 0,
        "market_size_series_periods": len(sources_data.get("market_size_series", {})) if isinstance(sources_data, dict) else 0,
        "hhi_series_periods": len(sources_data.get("hhi_series_5y", {})) if isinstance(sources_data, dict) else 0,
        "channel_data_count": len(sources_data.get("channel_data", {})) if isinstance(sources_data, dict) else 0,
        "specialty_data_count": len(sources_data.get("specialty_data", {})) if isinstance(sources_data, dict) else 0,
        "company_concentration_trend": summarize_value(company_conc),
        "company_concentration_periods": len(company_conc) if isinstance(company_conc, dict) else 0,
        "company_hhi_old_renderer_compatible": isinstance(company_conc, dict)
        and "periods" in company_conc
        and "hhi_values" in company_conc,
        "company_ranking_stacked": summarize_value(company_ranking),
        "company_ranking_periods": len(company_ranking) if isinstance(company_ranking, dict) else 0,
        "company_ranking_old_renderer_compatible": isinstance(company_ranking, dict) and "yearly" in company_ranking,
        "level_top5_trend": summarize_value(level_top5),
        "level_top5_levels": len(level_top5) if isinstance(level_top5, dict) else 0,
        "ei_ms_matrix": summarize_value(ei_ms),
        "ei_ms_matrix_rows": series_rows(ei_ms),
        "growth_contribution": summarize_value(growth),
        "growth_contribution_periods": len(growth) if isinstance(growth, dict) else 0,
        "growth_contribution_nonempty_periods": nonempty_periods(growth),
        "growth_company_contributors": path_len(data, "growth_contribution.by_company.top_contributors"),
        "growth_brand_contributors": path_len(data, "growth_contribution.by_brand.top_contributors"),
        "growth_contribution_ms_matrix": summarize_value(growth_ms),
        "growth_contribution_ms_rows": series_rows(growth_ms),
        "by_dimension_keys": list(data.get("by_dimension", {}).keys()) if isinstance(data.get("by_dimension"), dict) else [],
        "products_count": path_len(data, "by_dimension.products") if isinstance(data, dict) else 0,
    }


def summarize_deep_response(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        return {"body_type": type(body).__name__}
    data = body.get("data", {})
    forecast = data.get("forecast", {}) if isinstance(data, dict) else {}
    ai = data.get("ai_analysis", {}) if isinstance(data, dict) else {}
    simulation = data.get("simulation", {}) if isinstance(data, dict) else {}
    return {
        "top_keys": list(body.keys()),
        "data_keys": list(data.keys()) if isinstance(data, dict) else [],
        "brand_key": body.get("brand_key"),
        "view": body.get("view"),
        "source": body.get("source"),
        "measure": body.get("measure"),
        "forecast": summarize_value(forecast),
        "forecast_history_len": len(forecast.get("history", {})) if isinstance(forecast, dict) else 0,
        "forecast_predictions_len": len(forecast.get("predictions", [])) if isinstance(forecast, dict) else 0,
        "forecast_by_combo_len": len(forecast.get("by_combo", {})) if isinstance(forecast, dict) else 0,
        "forecast_by_combo_history_nonempty": sum(
            1
            for item in forecast.get("by_combo", {}).values()
            if isinstance(item, dict) and len(item.get("history_values", [])) > 0
        )
        if isinstance(forecast, dict)
        else 0,
        "events_len": len(data.get("events", [])) if isinstance(data, dict) and isinstance(data.get("events"), list) else 0,
        "ai_summary_present": bool(ai.get("summary")) if isinstance(ai, dict) else False,
        "ai_highlights_len": len(ai.get("highlights", [])) if isinstance(ai, dict) else 0,
        "simulation_scenarios_len": len(simulation.get("scenarios", [])) if isinstance(simulation, dict) else 0,
    }


def summarize_market_response(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        return {"body_type": type(body).__name__}
    data = body.get("data", body)
    return {
        "top_keys": list(body.keys()),
        "data_keys": list(data.keys()) if isinstance(data, dict) else [],
        "market_id": body.get("market_id") or data.get("market_id") if isinstance(data, dict) else body.get("market_id"),
        "view": body.get("view") or data.get("view") if isinstance(data, dict) else body.get("view"),
        "source": body.get("source") or data.get("source") if isinstance(data, dict) else body.get("source"),
        "measure": body.get("measure") or data.get("measure") if isinstance(data, dict) else body.get("measure"),
        "market_size_series": summarize_value(data.get("market_size_series") if isinstance(data, dict) else None),
        "hhi_series_5y": summarize_value(data.get("hhi_series_5y") if isinstance(data, dict) else None),
        "brand_ranking_stacked": summarize_value(data.get("brand_ranking_stacked") if isinstance(data, dict) else None),
        "company_ranking_stacked": summarize_value(data.get("company_ranking_stacked") if isinstance(data, dict) else None),
        "company_concentration_trend": summarize_value(
            data.get("company_concentration_trend") if isinstance(data, dict) else None
        ),
        "ei_ms_matrix": summarize_value(data.get("ei_ms_matrix") if isinstance(data, dict) else None),
        "growth_contribution": summarize_value(data.get("growth_contribution") if isinstance(data, dict) else None),
        "level_top5_trend": summarize_value(data.get("level_top5_trend") if isinstance(data, dict) else None),
        "target_customer_competition": summarize_value(
            data.get("target_customer_competition") if isinstance(data, dict) else None
        ),
    }


def brand_combos_for(item: dict[str, Any]) -> list[tuple[str, str, str]]:
    views = item.get("available_views") or VIEWS
    source_values = item.get("available_sources_normalized") or [
        normalize_source(src) for src in item.get("available_sources", [])
    ]
    source_values = sorted({src for src in source_values if src in SOURCE_MEASURES})
    if not source_values:
        source_values = SOURCES
    combos = []
    for view in views:
        if view not in VIEWS:
            continue
        for source in source_values:
            for measure in SOURCE_MEASURES[source]:
                combos.append((view, source, measure))
    return combos


def collect_cause_and_deep(brand_inventory: dict[str, Any]) -> dict[str, Any]:
    cause_dir = OUT_DIR / "cause"
    deep_dir = OUT_DIR / "deep"
    sample_dir = cause_dir / "samples"
    deep_sample_dir = deep_dir / "samples"
    for directory in [cause_dir, deep_dir, sample_dir, deep_sample_dir]:
        directory.mkdir(parents=True, exist_ok=True)

    brands = brand_inventory["jw_brand_list"]
    cause_results = []
    deep_results = []
    sample_brands = {"리바로", "리바로젯2", "리바로젯4", "가드메트", "페린젝트", "라베칸"}
    for item in brands:
        brand_key = item["brand_key"]
        combos = brand_combos_for(item)
        for view, source, measure in combos:
            params = {"view": view, "source": source, "measure": measure}
            status, body = fetch_json(f"/api/cause/{urllib.parse.quote(brand_key)}", params, timeout=45.0)
            summary = summarize_cause_response(body) if status == 200 else {"error": body}
            row = {"brand": brand_key, "view": view, "source": source, "measure": measure, "status": status, **summary}
            cause_results.append(row)
            if brand_key in sample_brands and (view, source, measure) in {
                ("strategic_ml", "ubist", "sales"),
                ("strategic_ml", "iqvia", "sales"),
                ("general", "iqvia", "sales"),
            }:
                write_json(sample_dir / f"{brand_key}_{view}_{source}_{measure}.json", {"status": status, "body": body})

            status, body = fetch_json(f"/api/deep-analysis/{urllib.parse.quote(brand_key)}", params, timeout=45.0)
            summary = summarize_deep_response(body) if status == 200 else {"error": body}
            deep_results.append({"brand": brand_key, "view": view, "source": source, "measure": measure, "status": status, **summary})
            if brand_key in sample_brands and (view, source, measure) in {
                ("strategic_ml", "ubist", "sales"),
                ("strategic_ml", "iqvia", "sales"),
                ("general", "iqvia", "sales"),
            }:
                write_json(
                    deep_sample_dir / f"{brand_key}_{view}_{source}_{measure}.json",
                    {"status": status, "body": body},
                )

    cause_summary = {
        "total_calls": len(cause_results),
        "status_counts": Counter(row["status"] for row in cause_results),
        "results": cause_results,
        "chart_fill_rates": chart_fill_rates(cause_results),
    }
    deep_summary = {
        "total_calls": len(deep_results),
        "status_counts": Counter(row["status"] for row in deep_results),
        "results": deep_results,
        "forecast_fill_rates": {
            "forecast_history_nonempty": sum(1 for row in deep_results if row.get("forecast_history_len", 0) > 0),
            "forecast_predictions_nonempty": sum(1 for row in deep_results if row.get("forecast_predictions_len", 0) > 0),
            "forecast_by_combo_nonempty": sum(1 for row in deep_results if row.get("forecast_by_combo_len", 0) > 0),
            "forecast_by_combo_history_nonempty": sum(
                1 for row in deep_results if row.get("forecast_by_combo_history_nonempty", 0) > 0
            ),
            "ai_summary_present": sum(1 for row in deep_results if row.get("ai_summary_present")),
            "ai_highlights_nonempty": sum(1 for row in deep_results if row.get("ai_highlights_len", 0) > 0),
        },
    }
    write_json(cause_dir / "cause_all_brands.json", cause_summary)
    write_json(OUT_DIR / "cause_all_brands.json", cause_summary)
    write_json(deep_dir / "deep_all_brands.json", deep_summary)
    write_json(OUT_DIR / "deep_all_brands.json", deep_summary)
    return {"cause": cause_summary, "deep": deep_summary}


def chart_fill_rates(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [row for row in rows if row.get("status") == 200]
    total = len(ok)
    if not total:
        return {}
    fields = [
        "metric_history_periods",
        "market_size_series_periods",
        "hhi_series_periods",
        "channel_data_count",
        "specialty_data_count",
        "company_concentration_periods",
        "company_ranking_periods",
        "level_top5_levels",
        "ei_ms_matrix_rows",
        "growth_contribution_periods",
        "growth_contribution_nonempty_periods",
        "growth_company_contributors",
        "growth_brand_contributors",
        "growth_contribution_ms_rows",
        "products_count",
    ]
    rates = {
        f"{field}_nonempty": sum(1 for row in ok if isinstance(row.get(field), int) and row.get(field, 0) > 0)
        for field in fields
    }
    rates["company_hhi_old_renderer_compatible"] = sum(1 for row in ok if row.get("company_hhi_old_renderer_compatible"))
    rates["company_ranking_old_renderer_compatible"] = sum(
        1 for row in ok if row.get("company_ranking_old_renderer_compatible")
    )
    rates["total_200"] = total
    return rates


def collect_market_status(db_checks: dict[str, Any], brand_inventory: dict[str, Any]) -> dict[str, Any]:
    market_dir = OUT_DIR / "market_status"
    sample_dir = market_dir / "samples"
    sample_dir.mkdir(parents=True, exist_ok=True)

    rows = db_checks.get("market_status_rows") or []
    if not rows:
        fallback = set()
        for brand in brand_inventory.get("jw_brand_list", []):
            for mid in brand.get("market_ids", []):
                for view, source, measure in brand_combos_for(brand):
                    fallback.add((view, mid, source, measure))
        rows = [
            {"view_type": view, "market_id": mid, "source": source, "measure": measure, "market_name": None}
            for view, mid, source, measure in sorted(fallback)
        ]

    results = []
    sample_markets = {"ml_006", "ml_012", "cd_006", "C10A1"}
    for row in rows:
        view = row["view_type"]
        source = normalize_source(row["source"])
        if source not in SOURCE_MEASURES:
            continue
        measure = row["measure"]
        market_id = row["market_id"]
        status, body = fetch_json(
            f"/api/market-status/{urllib.parse.quote(str(market_id))}",
            {"view": view, "source": source, "measure": measure},
            timeout=45.0,
        )
        summary = summarize_market_response(body) if status == 200 else {"error": body}
        results.append(
            {
                "market_id": market_id,
                "market_name": row.get("market_name"),
                "view": view,
                "source": source,
                "measure": measure,
                "status": status,
                **summary,
            }
        )
        if market_id in sample_markets and measure == "sales":
            write_json(sample_dir / f"{market_id}_{view}_{source}_{measure}.json", {"status": status, "body": body})

    summary = {
        "total_calls": len(results),
        "status_counts": Counter(row["status"] for row in results),
        "results": results,
        "path_nonempty_counts": {
            "market_size_series": sum(
                1 for row in results if row.get("market_size_series", {}).get("len", 0) > 0
            ),
            "company_concentration_trend": sum(
                1 for row in results if row.get("company_concentration_trend", {}).get("len", 0) > 0
            ),
            "growth_contribution": sum(1 for row in results if row.get("growth_contribution", {}).get("len", 0) > 0),
            "level_top5_trend": sum(1 for row in results if row.get("level_top5_trend", {}).get("len", 0) > 0),
            "ei_ms_matrix": sum(1 for row in results if row.get("ei_ms_matrix", {}).get("len", 0) > 0),
        },
    }
    write_json(market_dir / "market_status_all_markets.json", summary)
    write_json(OUT_DIR / "market_status_all_markets.json", summary)
    return summary


def md_table(rows: list[list[Any]], headers: list[str]) -> str:
    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for row in rows:
        out.append("| " + " | ".join(str(value).replace("\n", "<br>") for value in row) + " |")
    return "\n".join(out) + "\n"


def issue_line(label: str, root: str, evidence: str, fix: str) -> str:
    return f"### {label}\n- Root cause layer: {root}\n- Evidence: {evidence}\n- Fix direction: {fix}\n"


def write_audit(audit_dir: Path, summary: dict[str, Any]) -> None:
    audit_dir.mkdir(parents=True, exist_ok=True)
    raw_dest = audit_dir / "raw_data"
    if raw_dest.exists():
        shutil.rmtree(raw_dest)
    shutil.copytree(OUT_DIR, raw_dest, ignore=shutil.ignore_patterns("*.zip"))

    brand = summary["brand_inventory"]
    mockup = summary["mockup_static"]
    cause = summary["cause_deep"]["cause"]
    deep = summary["cause_deep"]["deep"]
    market = summary["market_status"]
    catalog = summary["catalog"]

    selected = brand["selected_mockup_brands"]
    selected_dual = [b for b in selected if set(b["available_sources_normalized"]) == {"iqvia", "ubist"}]
    selected_single = [b for b in selected if len(b["available_sources_normalized"]) == 1]
    selected_missing = brand["preferred_missing_in_api"]
    hidden_api = brand["api_jw_not_in_preferred"]

    cause_rates = cause.get("chart_fill_rates", {})
    deep_rates = deep.get("forecast_fill_rates", {})
    cause_total = cause_rates.get("total_200", 0)
    market_total = market.get("total_calls", 0)

    (audit_dir / "00_summary.md").write_text(
        "# Phase A-2-2-Side3 Diagnosis Summary\n\n"
        f"Generated: {datetime.now().isoformat()}\n\n"
        "## Issue Root Cause Classification\n\n"
        + issue_line(
            "Issue 1: Header 25 brands vs 18 cards",
            "Frontend mockup",
            f"`/api/brands` returns {brand['default_jw_total']} JW brands, but mockup `PREFERRED_BOOT_BRANDS` contains {len(PREFERRED_BOOT_BRANDS)} names and the selected card set is {brand['selected_mockup_brand_count']} cards. Header text still contains hardcoded `브랜드 25개` copy.",
            "Replace hardcoded 25-copy and either render all API JW brands or make the 18-card preferred subset explicit in the UI.",
        )
        + "\n"
        + issue_line(
            "Issue 2: Brand split by dosage/product-like unit",
            "Catalog/cache generation",
            "The API brand list already contains keys such as `리바로젯2`, `리바로젯4`, and `리바로페노2`; mockup displays `brand_key` directly. Catalog `strategic_brand` contains the same split names, so this is upstream of frontend rendering.",
            "Define canonical display brand grouping (`리바로젯`, `리바로페노`) in catalog/cache ETL, then rebuild cache. Frontend can optionally display canonical_name when provided.",
        )
        + "\n"
        + issue_line(
            "Issue 3: IQVIA/UBIST toggle visibility",
            "Mixed: API default scope + frontend boot query",
            f"Default `/api/brands` aggregates sources across cache and marks {len(selected_dual)}/{len(selected)} selected cards as dual-source; {len(selected_single)} selected cards are single-source. The mockup source toggle is conditional on `is_dual_source`, so the static code does not force toggles for every card.",
            "Boot brands with the active view/source or render source controls from per-view availability, not the all-cache default source union. Confirm PL saw toggle controls, not source badges.",
        )
        + "\n"
        + issue_line(
            "Issue 4: Empty cause/deep charts",
            "Mixed: frontend adapter mismatch plus intentional backend placeholders",
            f"Cause responses have metric history in {cause_rates.get('metric_history_periods_nonempty', 0)}/{cause_total} successful calls and EI/MS rows in {cause_rates.get('ei_ms_matrix_rows_nonempty', 0)}/{cause_total}, but old-renderer compatibility for company HHI is {cause_rates.get('company_hhi_old_renderer_compatible', 0)}/{cause_total}. Deep forecast history exists under `by_combo` in {deep_rates.get('forecast_by_combo_history_nonempty', 0)}/{deep['total_calls']} calls, while model predictions are nonempty in {deep_rates.get('forecast_predictions_nonempty', 0)}/{deep['total_calls']}.",
            "Patch frontend adapters for current `data.*` paths. Treat model predictions/AI prose as product-scope placeholders unless backend is asked to compute those outputs.",
        )
        + "\n"
        "## Artifacts\n\n"
        f"- Raw evidence: `{raw_dest}`\n"
        f"- Cause calls: {cause['total_calls']} ({dict(cause['status_counts'])})\n"
        f"- Deep-analysis calls: {deep['total_calls']} ({dict(deep['status_counts'])})\n"
        f"- Market-status calls: {market['total_calls']} ({dict(market['status_counts'])})\n"
        "\n## No-Change Confirmation\n\n"
        "- Backend code, mockup HTML, DB, catalog parquet were not modified by this phase.\n"
        "- Live Demo backend/proxy were left running.\n",
        encoding="utf-8",
    )

    brand_rows = [
        [
            b["brand_key"],
            ", ".join(b["available_sources"]),
            ", ".join(b["available_views"]),
            ", ".join(b["market_ids"][:6]),
        ]
        for b in brand["jw_brand_list"]
    ]
    (audit_dir / "01_brand_inventory.md").write_text(
        "# 01. Brand Inventory\n\n"
        f"- `/api/brands` total brands: {brand['default_total']:,}\n"
        f"- JW brands: {brand['default_jw_total']:,}\n"
        f"- Mockup selected cards: {brand['selected_mockup_brand_count']:,}\n"
        f"- Preferred list entries missing from API: {selected_missing}\n"
        f"- API JW brands not in preferred 18: {hidden_api}\n\n"
        "## JW Source Distribution\n\n"
        + md_table([[k, v] for k, v in brand["jw_source_distribution"].items()], ["available_sources", "brand_count"])
        + "\n## JW Brand List\n\n"
        + md_table(brand_rows, ["brand_key", "available_sources", "available_views", "market_ids"]),
        encoding="utf-8",
    )

    dose_rows = catalog.get("dose_split_rows", [])
    dose_table = md_table(
        [
            [
                row.get("brand_id"),
                row.get("name"),
                row.get("merge_name"),
                row.get("ml_id"),
                row.get("cd_id"),
                row.get("dosage_form"),
                row.get("strength_pack"),
            ]
            for row in dose_rows[:80]
        ],
        ["brand_id", "name", "merge_name", "ml_id", "cd_id", "dosage_form", "strength_pack"],
    )
    (audit_dir / "02_brand_segmentation.md").write_text(
        "# 02. Brand Segmentation\n\n"
        "The dosage/product-like segmentation is present before frontend rendering.\n\n"
        "## API Evidence\n\n"
        "- Selected preferred API keys include `리바로젯2`, `리바로젯4`, and `리바로페노2`.\n"
        "- `가드메트` is in the preferred frontend list, but is not present in the current default API brand list.\n\n"
        "## Catalog Evidence\n\n"
        f"- strategic_brand JW-like rows: {catalog.get('strategic_brand_jw_like_count')}\n"
        f"- strategic_product JW-like rows: {catalog.get('strategic_product_jw_like_count')}\n\n"
        "### Dose-split Rows\n\n"
        + dose_table
        + "\n## Diagnosis\n\n"
        "Root cause is catalog/cache brand-grain definition, not card rendering. The cache exposes split keys and the frontend faithfully displays them.\n",
        encoding="utf-8",
    )

    selected_rows = [
        [b["brand_key"], ", ".join(b["available_sources"]), ", ".join(b["available_sources_normalized"])]
        for b in selected
    ]
    (audit_dir / "03_source_availability.md").write_text(
        "# 03. Source Availability\n\n"
        f"- Selected cards: {len(selected)}\n"
        f"- Dual-source selected cards: {len(selected_dual)}\n"
        f"- Single-source selected cards: {len(selected_single)}\n\n"
        "## Selected Card Sources\n\n"
        + md_table(selected_rows, ["brand", "API available_sources", "normalized"])
        + "\n## Static Mockup Finding\n\n"
        "The source toggle is rendered under an `is_dual_source` condition. `is_dual_source` is derived from default `/api/brands`, which aggregates source availability across all cache scopes. The active page then fetches card data with a preferred source, so source controls can look broader than the current card payload.\n",
        encoding="utf-8",
    )

    chart_rows = [[key, value] for key, value in sorted(cause_rates.items())]
    market_rows = [[key, value] for key, value in sorted(market.get("path_nonempty_counts", {}).items())]
    deep_rows = [[key, value] for key, value in sorted(deep_rates.items())]
    (audit_dir / "04_chart_data_completeness.md").write_text(
        "# 04. Chart Data Completeness\n\n"
        f"Cause endpoint calls: {cause['total_calls']} / status counts {dict(cause['status_counts'])}\n\n"
        "## Cause Fill Rates\n\n"
        + md_table(chart_rows, ["field", "nonempty_or_compatible_count"])
        + "\n## Market-status Path Counts\n\n"
        + md_table(market_rows, ["path", "nonempty_count"])
        + "\n## Deep-analysis Fill Rates\n\n"
        + md_table(deep_rows, ["field", "count"])
        + "\n## Diagnosis\n\n"
        "- A.5 company concentration has period-keyed data, but the legacy renderer expects `periods/hhi_values`, so this is an adapter mismatch.\n"
        "- D.1 growth contribution is period-keyed and early periods can be empty; the renderer must adapt to the current period-map/list shape.\n"
        "- D.2 level-top5 data is present as level -> period maps; blank output points to frontend path/shape mismatch, not wholesale backend absence.\n"
        "- Deep-analysis has `forecast.by_combo.*.history_values`, while model predictions are intentionally empty and AI text is template-like. The history chart can be adapted; true prediction/AI output is a backend/product-scope gap.\n",
        encoding="utf-8",
    )

    (audit_dir / "05_catalog_vs_cache_mapping.md").write_text(
        "# 05. Catalog vs Cache Mapping\n\n"
        "## Catalog Tables\n\n"
        + md_table(
            [
                [table, info.get("rows"), ", ".join(info.get("columns", [])[:8])]
                for table, info in catalog.get("tables", {}).items()
            ],
            ["table", "rows", "first columns"],
        )
        + "\n## Cache Tables\n\n"
        + md_table(
            [
                [row["table_name"], f"{row['row_count']:,}"]
                for row in summary.get("db_checks", {}).get("table_counts", [])
            ],
            ["cache table", "rows"],
        )
        + "\n## Diagnosis\n\n"
        "The API reads cache tables only at runtime. Catalog evidence explains how cache keys were generated, but viewer/live API behavior must be fixed by catalog/cache rebuild or frontend adaptation, not by changing catalog parquet alone.\n",
        encoding="utf-8",
    )

    (audit_dir / "06_mockup_js_analysis.md").write_text(
        "# 06. Mockup JS Analysis\n\n"
        "## Key Findings\n\n"
        f"- `PREFERRED_BOOT_BRANDS` count: {mockup.get('preferred_boot_brand_count')}\n"
        "- Header/subtitle still contain hardcoded `브랜드 25개` copy.\n"
        "- Card count uses selected brand cards, not full API JW inventory.\n"
        "- Source toggle is conditional on `is_dual_source`.\n"
        "- Cause adapter bridges new API to old renderer, but some chart renderers still expect legacy shapes.\n\n"
        "## Relevant Raw Snippets\n\n"
        "See `raw_data/06_mockup_js_analysis.json` for exact line snippets around `selectPreferredApiBrands`, `unwrapNewCauseResponse`, source toggle, company HHI, and forecast rendering.\n",
        encoding="utf-8",
    )

    (audit_dir / "07_fix_recommendations.md").write_text(
        "# 07. Fix Recommendations\n\n"
        "## Frontend-only Fixes\n\n"
        "1. Replace hardcoded `브랜드 25개` copy with actual selected/full counts.\n"
        "2. Decide whether the status page is a curated 18-card demo or full JW inventory. If curated, label it explicitly.\n"
        "3. Source toggle should use per-card current-view source availability, not default all-cache source union.\n"
        "4. Update chart adapters for current `data.*` response paths, especially company concentration, growth contribution, level top5, and EI/MS matrix.\n\n"
        "## Backend / Cache / Catalog Fixes\n\n"
        "1. Add canonical brand grouping/display names for split products (`리바로젯2`/`리바로젯4` -> `리바로젯`, `리바로페노2` -> `리바로페노`) if PL wants brand-level cards.\n"
        "2. Rebuild cache after canonical grouping changes; live API is cache-only at runtime.\n"
        "3. Treat deep-analysis forecast/AI as a separate product feature unless placeholder responses are acceptable.\n\n"
        "## V3.1 Impact\n\n"
        "Frontend-only fixes do not require DB migration rollback. Canonical brand grouping or deep-analysis computation changes require local cache rebuild and then a DB/data refresh before production promotion.\n",
        encoding="utf-8",
    )


def make_zip(audit_dir: Path) -> Path:
    zip_path = audit_dir.with_suffix(".zip")
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in audit_dir.rglob("*"):
            zf.write(path, path.relative_to(audit_dir.parent))
    with zipfile.ZipFile(zip_path) as zf:
        bad = zf.testzip()
        if bad:
            raise RuntimeError(f"zip verification failed at {bad}")
    return zip_path


def collect_all(audit_dir: Path | None = None) -> dict[str, Any]:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    started = time.time()
    preflight = collect_preflight()
    db_checks = collect_db_checks()
    brand_inventory = collect_brands()
    catalog = collect_catalog()
    mockup_static = collect_mockup_static()
    cause_deep = collect_cause_and_deep(brand_inventory)
    market_status = collect_market_status(db_checks, brand_inventory)

    summary = {
        "generated_at": datetime.now().isoformat(),
        "elapsed_sec": round(time.time() - started, 2),
        "api_base": API_BASE,
        "preflight": preflight,
        "db_checks": db_checks,
        "brand_inventory": brand_inventory,
        "catalog": catalog,
        "mockup_static": mockup_static,
        "cause_deep": cause_deep,
        "market_status": market_status,
    }
    write_json(OUT_DIR / "diagnosis_summary.json", summary)
    if audit_dir is None:
        audit_dir = REPO_ROOT / f"phase_a2_2_side3_diagnosis_{now_tag()}"
    write_audit(audit_dir, summary)
    zip_path = make_zip(audit_dir)
    summary["audit_dir"] = str(audit_dir)
    summary["audit_zip"] = str(zip_path)
    write_json(OUT_DIR / "diagnosis_summary.json", summary)
    return summary


def test_live_demo_health() -> None:
    status, body = fetch_json("/api/health", timeout=10.0)
    assert status == 200
    assert isinstance(body, dict)
    assert body.get("status") == "ok"


def test_existing_diagnosis_outputs_are_valid() -> None:
    summary_path = OUT_DIR / "diagnosis_summary.json"
    if not summary_path.exists():
        pytest.skip("Run this file as a script to generate /tmp/jw_diagnosis first.")
    summary = read_json(summary_path)
    assert summary["brand_inventory"]["default_jw_total"] > 0
    assert summary["cause_deep"]["cause"]["total_calls"] > 0
    assert summary["cause_deep"]["deep"]["total_calls"] > 0
    assert summary["market_status"]["total_calls"] > 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    summary = collect_all(args.audit_dir)
    print(json.dumps({"audit_dir": summary["audit_dir"], "audit_zip": summary["audit_zip"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
