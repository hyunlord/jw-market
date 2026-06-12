#!/usr/bin/env python3
"""Build and load source-aware JSON Layer 3 general-view marts.

Phase 16-G-4-Fix-Load changes the v3 dry-run prototype into an insertable
pipeline. General view keeps all raw brands, deduplicates rows by
``brand_key × atc4 × source × measure``, and preserves product-level detail in
``by_dimension.products``.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import duckdb
import pandas as pd
import pymysql

from brand_key_normalize import best_name, extract_brand_base_name, normalize_brand_name
from dict_ubist_translation import CHANNEL_CODE_TO_RAW, SPECIALTY_CODE_TO_RAW
from layer3_compute_extended import compute_cagr_value, compute_ei, compute_growth_contribution, compute_hhi, compute_momentum
from layer3_compute_market_metric import compute_market_mart_payload
from layer3_normalize import period_range_mat, period_sort_key, prev_month, prev_quarter_month, safe_div, same_month_prev_year
from ops_utils import configure_logging, find_project_root, first_existing, retry
from resolve_company import resolve_company
try:
    from pipeline.scripts.utils.ubist_channel_mapping import STANDALONE_INTERNAL_MEDICINE_SPECIALTY
except ModuleNotFoundError:  # pragma: no cover - direct script execution path
    from utils.ubist_channel_mapping import STANDALONE_INTERNAL_MEDICINE_SPECIALTY


LOGGER = configure_logging(__name__)
PROJECT_ROOT = find_project_root(Path(__file__).resolve())
OUTPUT_DIR = PROJECT_ROOT / "output"
CATALOG_DIR = OUTPUT_DIR / "catalog"
ENRICHED_GLOB = str(OUTPUT_DIR / "enriched" / "ml_id=*" / "data.parquet")
UBIST_GLOB = str(OUTPUT_DIR / "ubist" / "year=*" / "month=*" / "data.parquet")
DRY_RUN_DIR = Path("/tmp")
ALLOWED_SOURCES = ("ubist", "iqvia_nsa")
MEASURES_BY_SOURCE = {
    "ubist": ("sales", "volume"),
    "iqvia_nsa": ("sales", "unit", "dosage_unit", "counting_unit"),
}
UNIT_LABELS = {
    ("ubist", "sales"): "KRW",
    ("ubist", "volume"): "Rx",
    ("iqvia_nsa", "sales"): "KRW",
    ("iqvia_nsa", "unit"): "unit",
    ("iqvia_nsa", "dosage_unit"): "dosage unit",
    ("iqvia_nsa", "counting_unit"): "counting unit",
}
GENERAL_BRAND_INSERT_COLUMNS = [
    "brand_key",
    "brand_name",
    "atc4_code",
    "atc4_desc",
    "source",
    "measure",
    "unit_label",
    "metric_history",
    "extended_metric_history",
    "channel_data",
    "specialty_data",
    "dimension_data",
    "dimension_channel_data",
    "by_dimension",
    "raw_value_history",
    "payload",
]
GENERAL_MARKET_INSERT_COLUMNS = [
    "atc4_code",
    "atc4_desc",
    "source",
    "measure",
    "unit_label",
    "market_size_series",
    "hhi_series",
    "brand_ranking",
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
JSON_INSERT_COLUMNS = {
    "metric_history",
    "extended_metric_history",
    "channel_data",
    "specialty_data",
    "dimension_data",
    "dimension_channel_data",
    "dimension_specialty_data",
    "by_dimension",
    "raw_value_history",
    "market_size_series",
    "hhi_series",
    "hhi_series_5y",
    "brand_ranking",
    "brand_ranking_stacked",
    "company_ranking_stacked",
    "company_concentration_trend",
    "ei_ms_matrix",
    "growth_contribution_ms_matrix",
    "growth_contribution",
    "analysis_levels",
    "level_top5_trend",
    "target_customer_competition",
    "overlay_data",
    "cd_overlay",
    "payload",
}
SKU_DIMENSION_COLUMNS = ("nhi_type", "molecule", "dosage_form", "strength_pack", "ox_gx", "fish_oil")


def general_brand_jsonl_path(source: str, output_dir: Path | None = None) -> Path:
    return (output_dir or DRY_RUN_DIR) / f"general_v3_{source}_brand_rows.jsonl"


def general_market_jsonl_path(source: str, output_dir: Path | None = None) -> Path:
    return (output_dir or DRY_RUN_DIR) / f"general_v3_{source}_market_rows.jsonl"


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        # Stage 빌드/복구 환경에서는 pipeline/docker/.env가 없고 컨테이너가
        # MARIADB_* 환경변수만 제공하는 경우가 있다. 이때도 official script를
        # 그대로 쓰기 위해 env fallback을 허용한다. 별도 staging harness를
        # 만드는 대안은 운영 빌더 경로 검증을 흐리므로 기각했다.
        return {key: value for key, value in os.environ.items() if key.startswith("MARIADB_") or key == "HOST_PORT"}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    # .env를 읽은 뒤에도 shell env가 있으면 그 값을 우선한다.
    # 로컬 live와 staging schema를 같은 script로 오갈 때 필요한 override이며,
    # 파일을 직접 수정하는 방식은 보호 파일 drift를 만들기 때문에 기각했다.
    for key in ("MARIADB_HOST", "MARIADB_PORT", "MARIADB_DATABASE", "MARIADB_USER", "MARIADB_PASSWORD", "HOST_PORT"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


@retry((pymysql.err.OperationalError, pymysql.err.InterfaceError), logger=LOGGER)
def mariadb_connect(cursorclass=pymysql.cursors.DictCursor) -> pymysql.connections.Connection:
    env_path = first_existing(PROJECT_ROOT / "pipeline" / "docker" / ".env", PROJECT_ROOT / "docker" / ".env")
    env = load_env(env_path)
    if "MARIADB_PASSWORD" not in env:
        raise RuntimeError(f"MARIADB_PASSWORD is missing in {env_path}")
    return pymysql.connect(
        # HOST/PORT도 env로 열어 staging DB와 recover DB를 같은 코드 경로에서
        # 다룬다. host를 127.0.0.1로 고정하는 대안은 docker recover 환경에서
        # 접속 경로를 바꾸기 어렵게 해 기각했다.
        host=env.get("MARIADB_HOST", "127.0.0.1"),
        port=int(env.get("MARIADB_PORT") or env.get("HOST_PORT", "3307")),
        user=env.get("MARIADB_USER", "jwapp"),
        password=env["MARIADB_PASSWORD"],
        database=env.get("MARIADB_DATABASE", "jw_mart"),
        charset="utf8mb4",
        autocommit=True,
        cursorclass=cursorclass,
    )


def json_ready(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): json_ready(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_ready(v) for v in value]
    if isinstance(value, tuple):
        return [json_ready(v) for v in value]
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            return value
    return value


def dumps(value: Any) -> str:
    return json.dumps(json_ready(value), ensure_ascii=False, separators=(",", ":"))


def ensure_json_columns(table: str, columns: Iterable[str]) -> None:
    """Add JSON columns required by newer mart writers when an existing DB is reused."""
    conn = mariadb_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SHOW COLUMNS FROM {table}")
            existing = {row["Field"] for row in cur.fetchall()}
            for column in columns:
                if column not in existing:
                    cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} JSON NULL")
    finally:
        conn.close()


def safe_float(value: Any) -> float:
    try:
        if value is None or pd.isna(value):
            return 0.0
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            number = float(value)
            if math.isnan(number) or math.isinf(number):
                return 0.0
            return number
        number = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(number) or math.isinf(number):
        return 0.0
    return number


def extract_atc4(value: Any) -> tuple[str, str | None]:
    if value is None or pd.isna(value):
        return "UNKNOWN", None
    text = str(value).strip()
    if not text:
        return "UNKNOWN", None
    match = re.search(r"\[?([A-Z][0-9A-Z]{2,5})\]?", text.upper())
    code = match.group(1) if match else text.split("_", 1)[0].split()[0].strip("[]").upper()
    return code or "UNKNOWN", text


def normalise_iqvia_channel(audit_code: Any) -> str | None:
    text = str(audit_code or "").upper()
    if text.startswith("KHPA"):
        return "KHPA"
    if text.startswith("KCPA"):
        return "KCPA"
    if text.startswith("KPA"):
        return "KPA"
    return None


def ubist_channel_to_raw(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "분리되지 않은 종별"
    return CHANNEL_CODE_TO_RAW.get(text, text if any("\uac00" <= ch <= "\ud7a3" for ch in text) else "분리되지 않은 종별")


def ubist_specialty_to_raw(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "분리되지 않은 진료과"
    if any("\uac00" <= ch <= "\ud7a3" for ch in text):
        return text
    for code, raws in SPECIALTY_CODE_TO_RAW.items():
        if text == code:
            return raws[0]
    return "분리되지 않은 진료과"


def deduplicate_ubist_internal_medicine_rows(frame: pd.DataFrame) -> pd.DataFrame:
    """Drop standalone 내과(IM) rows before UBIST aggregation.

    무엇: UBIST raw의 standalone ``내과(IM)`` 행만 제거하고 세부 10개 내과
    specialty는 보존한다.
    왜: PL 검증에서 standalone 내과가 세부 10개 합과 등가라 같이 더하면
    시장 총합이 약 40% 과대 집계된다.
    도메인 근거: 내과 표시는 세부 10개 합으로 재구성하고, standalone은
    중복 원천 행이다.
    기각 대안: cache 화면에서만 감추면 mart 시장 총합/MS/HHI 과대가 남는다.
    """
    if frame.empty or "specialty" not in frame.columns:
        return frame
    mask = frame["specialty"].astype(str).str.strip() == STANDALONE_INTERNAL_MEDICINE_SPECIALTY
    if not mask.any():
        return frame
    return frame.loc[~mask].copy()


def normalize_period_label(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    match = re.match(r"^(\d{4})Q([1-4])$", text)
    if match:
        return f"{match.group(1)}-Q{match.group(2)}"
    return text


def iqvia_source_priority(source_file: Any) -> int:
    """Prefer the newest overlapping NSA extract for duplicated period rows."""
    text = str(source_file or "")
    match = re.search(r"(20\d{2})\s*(?:_| )?([1-4])Q", text, flags=re.IGNORECASE)
    if not match:
        match = re.search(r"([1-4])Q\s*(20\d{2})", text, flags=re.IGNORECASE)
        if match:
            quarter, year = int(match.group(1)), int(match.group(2))
            return year * 10 + quarter
        return 0
    year, quarter = int(match.group(1)), int(match.group(2))
    return year * 10 + quarter


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(dumps(row) + "\n")


def load_catalog_key_map() -> dict[str, dict[str, Any]]:
    catalog = pd.read_parquet(CATALOG_DIR / "strategic_brand" / "strategic_brand.parquet")
    mapping: dict[str, dict[str, Any]] = {}
    for _, row in catalog.iterrows():
        for col in ("name", "merge_name"):
            key = normalize_brand_name(row.get(col))
            if key and key not in mapping:
                mapping[key] = row.to_dict()
    return mapping


def load_ubist_base_frame(max_rows: int | None = None, ml: str | None = None) -> pd.DataFrame:
    if ml is None:
        limit = f"LIMIT {int(max_rows)}" if max_rows else ""
        query = f"""
            SELECT
              CAST("약품코드" AS VARCHAR) AS product_code,
              first("제품") AS product_name,
              first("브랜드") AS brand_name,
              first("ATC") AS atc_text,
              period_yyyymm,
              "종별" AS channel,
              "진료과" AS specialty,
              first("제조사") AS manufacturer,
              first("판매사") AS company,
              SUM(TRY_CAST(rx_amt AS DOUBLE)) AS raw_sales,
              SUM(TRY_CAST(rx_qty AS DOUBLE)) AS raw_volume
            FROM (
              SELECT *
              FROM read_parquet('{UBIST_GLOB}', hive_partitioning=true)
              WHERE TRY_CAST(rx_amt AS DOUBLE) > 0 OR TRY_CAST(rx_qty AS DOUBLE) > 0
              {limit}
            ) AS u
            GROUP BY 1,5,6,7
        """
        LOGGER.info("[ubist] aggregating raw UBIST parquet for all ATC4")
        con = duckdb.connect()
        try:
            frame = con.execute(query).df()
        finally:
            con.close()
        frame["source"] = "ubist"
        frame["brand_name"] = frame.apply(
            lambda r: best_name(
                extract_brand_base_name(r.get("product_name")),
                r.get("brand_name"),
                r.get("product_code"),
            ),
            axis=1,
        )
        frame["brand_key"] = frame["brand_name"].map(normalize_brand_name)
        atc = frame["atc_text"].map(extract_atc4)
        frame["atc4_code"] = atc.map(lambda pair: pair[0])
        frame["atc4_desc"] = atc.map(lambda pair: pair[1])
        frame["channel"] = frame["channel"].map(ubist_channel_to_raw)
        frame["specialty"] = frame["specialty"].map(ubist_specialty_to_raw)
        frame = deduplicate_ubist_internal_medicine_rows(frame)
        return frame.loc[frame["brand_key"] != ""].copy()

    limit = f"LIMIT {int(max_rows)}" if max_rows else ""
    parquet_glob = str(OUTPUT_DIR / "enriched" / f"ml_id={ml}" / "data.parquet") if ml else ENRICHED_GLOB
    query = f"""
        SELECT
          product_id,
          split_part(source_row_id, '::', 6) AS product_code,
          period_yyyymm,
          channel,
          specialty,
          SUM(CAST(raw_rx_amt AS DOUBLE)) AS raw_sales,
          SUM(CAST(raw_rx_qty AS DOUBLE)) AS raw_volume
        FROM (
          SELECT *
          FROM read_parquet('{parquet_glob}')
          WHERE source='ubist' AND (TRY_CAST(raw_rx_amt AS DOUBLE) > 0 OR TRY_CAST(raw_rx_qty AS DOUBLE) > 0)
          {limit}
        ) AS e
        GROUP BY 1,2,3,4,5
    """
    LOGGER.info("[ubist] aggregating Layer 2 parquet")
    con = duckdb.connect()
    try:
        frame = con.execute(query).df()
    finally:
        con.close()
    products = pd.read_parquet(CATALOG_DIR / "strategic_product" / "strategic_product.parquet")
    products = products.rename(columns={"name": "product_name", "merge_name": "brand_name"})
    keep = ["product_id", "product_name", "brand_name", "brand_id", "class", "molecule", "dosage_form", "strength_pack", "nhi_type", "ox_gx", "fish_oil", "판매사", "제조사"]
    frame = frame.merge(products[[c for c in keep if c in products.columns]].drop_duplicates("product_id"), on="product_id", how="left")
    frame["source"] = "ubist"
    frame["brand_name"] = frame.apply(lambda r: best_name(r.get("brand_name"), r.get("product_name"), r.get("product_id")), axis=1)
    frame["brand_key"] = frame["brand_name"].map(normalize_brand_name)
    codes = [code for code in frame["product_code"].dropna().astype(str).unique().tolist() if code]
    atc_map: dict[str, tuple[str, str | None]] = {}
    if codes:
        con = duckdb.connect()
        con.register("codes", pd.DataFrame({"product_code": codes}))
        try:
            mapping = con.execute(
                f"""
                SELECT CAST(u.약품코드 AS VARCHAR) AS product_code, first(u.ATC) AS atc_text
                FROM read_parquet('{UBIST_GLOB}') AS u
                JOIN codes AS c ON CAST(u.약품코드 AS VARCHAR)=c.product_code
                GROUP BY 1
                """
            ).df()
        finally:
            con.close()
        atc_map = {row["product_code"]: extract_atc4(row["atc_text"]) for _, row in mapping.iterrows()}
    atc = frame["product_code"].map(lambda code: atc_map.get(str(code), ("UNKNOWN", None)))
    frame["atc4_code"] = atc.map(lambda pair: pair[0])
    frame["atc4_desc"] = atc.map(lambda pair: pair[1])
    frame["manufacturer"] = frame.get("제조사")
    frame["company"] = frame.get("판매사")
    frame["channel"] = frame["channel"].map(ubist_channel_to_raw)
    frame["specialty"] = frame["specialty"].map(ubist_specialty_to_raw)
    frame = deduplicate_ubist_internal_medicine_rows(frame)
    return frame.loc[frame["brand_key"] != ""].copy()


def ubist_measure_frame(base: pd.DataFrame, measure: str) -> pd.DataFrame:
    frame = base.copy()
    frame["measure"] = measure
    frame["raw_value"] = frame["raw_sales"] if measure == "sales" else frame["raw_volume"]
    return frame.loc[frame["raw_value"].notna() & (frame["raw_value"] > 0)].copy()


def load_iqvia_base_frame(max_rows: int | None = None) -> pd.DataFrame:
    limit = f" LIMIT {int(max_rows)}" if max_rows else ""
    LOGGER.info("[iqvia_nsa] fetching raw rows%s", f" limit={max_rows}" if max_rows else "")
    conn = mariadb_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, source_file, source_row_no, audit_code, mfr_name, period_label, payload "
                f"FROM iqvia_nsa_quarterly_raw{limit}"
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
    LOGGER.info("[iqvia_nsa] de-duplicated overlapping extracts rows=%s -> %s", f"{before:,}", f"{len(frame):,}")
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


def fill_periods(periods: Iterable[str]) -> list[str]:
    return sorted({str(period) for period in periods if period}, key=period_sort_key)


def period_value_map(group: pd.DataFrame, periods: list[str]) -> dict[str, float]:
    series = group.groupby("period_yyyymm", dropna=False)["raw_value"].sum().to_dict()
    return {period: float(series.get(period, 0.0) or 0.0) for period in periods}


def value_at(history: dict[str, float], period: str | None) -> float | None:
    if not period:
        return None
    return history.get(period)


def pct_growth(current: float | None, previous: float | None) -> float | None:
    ratio = safe_div(current, previous)
    if ratio is None:
        return None
    return (ratio - 1.0) * 100


def mat_growth(history: dict[str, float], period: str) -> float | None:
    window = period_range_mat(period)
    previous_end = same_month_prev_year(period)
    previous_window = period_range_mat(previous_end) if previous_end else []
    if not window or not previous_window:
        return None
    return pct_growth(sum(history.get(p, 0.0) for p in window), sum(history.get(p, 0.0) for p in previous_window))


def cagr_from_history(history: dict[str, float], period: str, years: int) -> float | None:
    try:
        ord_now = period_sort_key(period)
    except Exception:
        return None
    periods_per_year = 4 if "-Q" in period else 12
    target_ord = ord_now - periods_per_year * years
    start_period = next((p for p in history if period_sort_key(p) == target_ord), None)
    return compute_cagr_value(history.get(period), history.get(start_period) if start_period else None, years)


def hhi_for_period(part: pd.DataFrame) -> float | None:
    total = part["raw_value"].sum()
    if total <= 0:
        return None
    values = part.groupby("brand_key")["raw_value"].sum()
    return compute_hhi([(value / total) for value in values if value > 0])


def build_dimensional_history(group: pd.DataFrame, dim_col: str, periods: list[str]) -> dict[str, dict[str, dict[str, float]]]:
    if dim_col not in group.columns:
        return {}
    result: dict[str, dict[str, dict[str, float]]] = {}
    for label, part in group.groupby(dim_col, dropna=False):
        if label is None or pd.isna(label) or not str(label).strip():
            continue
        values = period_value_map(part, periods)
        result[str(label)] = {period: {"raw_value": value} for period, value in values.items()}
    return result


def build_dimension_channel_history(
    group: pd.DataFrame,
    dim_col: str,
    periods: list[str],
) -> dict[str, dict[str, dict[str, dict[str, float]]]]:
    if dim_col not in group.columns or "channel" not in group.columns:
        return {}
    result: dict[str, dict[str, dict[str, dict[str, float]]]] = {}
    for (label, channel), part in group.groupby([dim_col, "channel"], dropna=False):
        if label is None or pd.isna(label) or not str(label).strip():
            continue
        if channel is None or pd.isna(channel) or not str(channel).strip():
            continue
        values = period_value_map(part, periods)
        result.setdefault(str(label), {})[str(channel)] = {
            period: {"raw_value": value} for period, value in values.items()
        }
    return result


def build_sku_dimension_data(group: pd.DataFrame, periods: list[str]) -> dict[str, dict[str, dict[str, dict[str, float]]]]:
    return {
        dim_col: build_dimensional_history(group, dim_col, periods)
        for dim_col in SKU_DIMENSION_COLUMNS
        if dim_col in group.columns
    }


def build_sku_dimension_channel_data(group: pd.DataFrame, periods: list[str]) -> dict[str, dict[str, dict[str, dict[str, dict[str, float]]]]]:
    return {
        dim_col: build_dimension_channel_history(group, dim_col, periods)
        for dim_col in SKU_DIMENSION_COLUMNS
        if dim_col in group.columns
    }


def build_channel_specialty_matrix(group: pd.DataFrame, periods: list[str]) -> dict[str, dict[str, dict[str, float]]]:
    result: dict[str, dict[str, dict[str, float]]] = {}
    if "channel" not in group.columns or "specialty" not in group.columns:
        return result
    for (channel, specialty), part in group.groupby(["channel", "specialty"], dropna=False):
        if pd.isna(channel) or pd.isna(specialty):
            continue
        result.setdefault(str(channel), {})[str(specialty)] = period_value_map(part, periods)
    return result


def build_products(group: pd.DataFrame, periods: list[str]) -> list[dict[str, Any]]:
    products = []
    for (product_name, product_code), part in group.groupby(["product_name", "product_code"], dropna=False):
        if pd.isna(product_name):
            continue
        history = period_value_map(part, periods)
        products.append(
            {
                "product_name": str(product_name),
                "product_code": None if pd.isna(product_code) else str(product_code),
                "raw_value_total": float(sum(history.values())),
                "raw_value_history": history,
            }
        )
    return sorted(products, key=lambda item: item["raw_value_total"], reverse=True)


def build_brand_rows(source: str, measure: str, frame: pd.DataFrame, catalog_map: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    working = frame.loc[frame["raw_value"].notna() & (frame["raw_value"] > 0)].copy()
    if working.empty:
        return []
    market_periods = {
        atc: fill_periods(part["period_yyyymm"].unique())
        for atc, part in working.groupby("atc4_code", dropna=False)
    }
    market_period_totals = working.groupby(["atc4_code", "period_yyyymm"], dropna=False)["raw_value"].sum().to_dict()
    market_history_by_atc = {
        atc: period_value_map(part, market_periods[atc])
        for atc, part in working.groupby("atc4_code", dropna=False)
    }
    hhi_by_atc_period = {
        (str(atc), str(period)): hhi_for_period(part)
        for (atc, period), part in working.groupby(["atc4_code", "period_yyyymm"], dropna=False)
    }
    rank_lookup: dict[tuple[str, str, str], int] = {}
    rank_source = working.groupby(["atc4_code", "period_yyyymm", "brand_key"], dropna=False)["raw_value"].sum().reset_index()
    for (atc, period), part in rank_source.groupby(["atc4_code", "period_yyyymm"], dropna=False):
        part = part.sort_values("raw_value", ascending=False).reset_index(drop=True)
        for idx, row in part.iterrows():
            rank_lookup[(str(atc), str(period), str(row["brand_key"]))] = int(idx + 1)
    rows: list[dict[str, Any]] = []
    for (brand_key, atc4_code), group in working.groupby(["brand_key", "atc4_code"], dropna=False):
        periods = market_periods.get(atc4_code, fill_periods(group["period_yyyymm"].unique()))
        history = period_value_map(group, periods)
        atc_history = market_history_by_atc.get(atc4_code, {})
        metric_history: dict[str, dict[str, Any]] = {}
        extended_history: dict[str, dict[str, Any]] = {}
        ms_values: list[float] = []
        for period in periods:
            value = history.get(period, 0.0)
            market_total = market_period_totals.get((atc4_code, period), 0.0)
            ms = safe_div(value, market_total)
            ms_pct = ms * 100 if ms is not None else 0.0
            ms_values.append(ms_pct)
            prev = value_at(history, prev_month(period))
            prev_q = value_at(history, prev_quarter_month(period))
            prev_y = value_at(history, same_month_prev_year(period))
            growth_abs = value - prev_y if prev_y is not None else None
            market_prev_y = value_at(atc_history, same_month_prev_year(period))
            market_growth_abs = atc_history.get(period, 0.0) - market_prev_y if market_prev_y is not None else None
            gc, gc_warning = compute_growth_contribution(growth_abs, market_growth_abs)
            cagr_5y = cagr_from_history(history, period, 5)
            market_cagr_5y = cagr_from_history(atc_history, period, 5)
            ei_5y, ei_warning = compute_ei(cagr_5y, market_cagr_5y)
            rank = rank_lookup.get((str(atc4_code), str(period), str(brand_key)))
            metric_history[period] = {
                "raw_value": value,
                "ms": ms_pct,
                "mom": pct_growth(value, prev),
                "qoq": pct_growth(value, prev_q),
                "yoy": pct_growth(value, prev_y),
                "mat": mat_growth(history, period),
                "growth_abs": growth_abs,
                "rank": rank,
            }
            extended_history[period] = {
                "cagr_1y": cagr_from_history(history, period, 1),
                "cagr_3y": cagr_from_history(history, period, 3),
                "cagr_5y": cagr_5y,
                "ei_5y": ei_5y,
                "momentum_score": compute_momentum(ms_values[-4:]) if len(ms_values) >= 4 else None,
                "growth_contribution": gc,
                "growth_contribution_pct": gc,
                "hhi": hhi_by_atc_period.get((str(atc4_code), period)),
                "market_cagr_5y": market_cagr_5y,
                "warnings": [w for w in (gc_warning, ei_warning) if w],
            }
        first = group.iloc[0].to_dict()
        catalog_row = catalog_map.get(str(brand_key))
        company = resolve_company(catalog_row, first, source)
        by_dimension = {
            "company": company,
            "manufacturer": first.get("manufacturer"),
            "raw_company": first.get("company"),
            "products": build_products(group, periods),
            "catalog_status": "matched" if catalog_row else "unmatched",
            "catalog_brand_id": catalog_row.get("brand_id") if catalog_row else None,
            "atc4_code": str(atc4_code),
            "atc4_desc": first.get("atc4_desc"),
        }
        rows.append(
            {
                "brand_key": str(brand_key),
                "brand_name": best_name(first.get("brand_name"), brand_key),
                "atc4_code": str(atc4_code),
                "atc4_desc": first.get("atc4_desc"),
                "source": source,
                "measure": measure,
                "unit_label": UNIT_LABELS[(source, measure)],
                "metric_history": metric_history,
                "extended_metric_history": extended_history,
                "channel_data": build_dimensional_history(group, "channel", periods),
                "specialty_data": build_dimensional_history(group, "specialty", periods) if source == "ubist" else {},
                "channel_specialty_matrix": build_channel_specialty_matrix(group, periods) if source == "ubist" else {},
                "dimension_data": build_sku_dimension_data(group, periods),
                "dimension_channel_data": build_sku_dimension_channel_data(group, periods),
                "by_dimension": by_dimension,
                "raw_value_history": history,
                "payload": {
                    "phase": "16-G-4-Fix-Load",
                    "etl_version": "v3.1",
                    "computed_at": datetime.now().isoformat(timespec="seconds"),
                    "row_count": int(len(group)),
                    "period_count": int(len(periods)),
                },
            }
        )
    return rows


def build_market_rows(source: str, measure: str, brand_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for atc4_code, grouped in _group_rows(brand_rows, "atc4_code").items():
        atc4_desc = next((row.get("atc4_desc") for row in grouped if row.get("atc4_desc")), None)
        payload = compute_market_mart_payload(grouped, source=source, measure=measure, view_type="general", catalog_market_row=None)
        rows.append(
            {
                "atc4_code": atc4_code,
                "atc4_desc": atc4_desc,
                "source": source,
                "measure": measure,
                "unit_label": UNIT_LABELS[(source, measure)],
                "market_size_series": payload["market_size_series"],
                "hhi_series": payload["hhi_series_5y"],
                "brand_ranking": payload["brand_ranking_stacked"],
                "company_ranking_stacked": payload["company_ranking_stacked"],
                "company_concentration_trend": payload["company_concentration_trend"],
                "ei_ms_matrix": payload["ei_ms_matrix"],
                "growth_contribution_ms_matrix": payload["growth_contribution_ms_matrix"],
                "growth_contribution": payload["growth_contribution"],
                "analysis_levels": None,
                "level_top5_trend": None,
                "target_customer_competition": payload["target_customer_competition"],
                "payload": payload["payload"],
            }
        )
    return rows


def _group_rows(rows: list[dict[str, Any]], key: str) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get(key))].append(row)
    return grouped


def restrict_atc4(frame: pd.DataFrame, limit_atc4: int | None) -> pd.DataFrame:
    if not limit_atc4:
        return frame
    values = sorted(v for v in frame["atc4_code"].dropna().unique().tolist() if v != "UNKNOWN")[:limit_atc4]
    return frame.loc[frame["atc4_code"].isin(values)].copy()


def iter_atc4_chunks(frame: pd.DataFrame, limit_atc4: int | None = None) -> Iterable[tuple[str, pd.DataFrame]]:
    if frame.empty:
        return
    if limit_atc4:
        values = sorted(v for v in frame["atc4_code"].dropna().unique().tolist() if v != "UNKNOWN")[:limit_atc4]
        frame = frame.loc[frame["atc4_code"].isin(values)]
    for atc4_code, chunk in frame.groupby("atc4_code", dropna=False, sort=True):
        if chunk.empty:
            continue
        chunk_key = "nan" if pd.isna(atc4_code) else str(atc4_code)
        yield chunk_key, chunk.copy()


def insert_rows(table: str, columns: list[str], rows: list[dict[str, Any]], batch_size: int = 500) -> None:
    if not rows:
        return
    placeholders = ",".join(["%s"] * len(columns))
    col_sql = ",".join(columns)
    update_cols = [col for col in columns if col not in {"brand_key", "atc4_code", "source", "measure"}]
    update_sql = ",".join([f"{col}=VALUES({col})" for col in update_cols])
    sql = f"INSERT INTO {table} ({col_sql}) VALUES ({placeholders}) ON DUPLICATE KEY UPDATE {update_sql}"
    payloads = []
    for row in rows:
        payloads.append(
            tuple(
                dumps(row.get(col)) if col in JSON_INSERT_COLUMNS else row.get(col)
                for col in columns
            )
        )
    conn = mariadb_connect()
    try:
        with conn.cursor() as cur:
            for start in range(0, len(payloads), batch_size):
                cur.executemany(sql, payloads[start : start + batch_size])
    finally:
        conn.close()


def delete_source_rows(table: str, source: str) -> None:
    conn = mariadb_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"DELETE FROM {table} WHERE source=%s", (source,))
    finally:
        conn.close()


def compute_general(
    source: str,
    dry_run: bool = False,
    insert: bool = False,
    limit_atc4: int | None = None,
    max_rows: int | None = None,
    output_dir: Path | None = None,
    ml: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    if source not in ALLOWED_SOURCES:
        raise ValueError(f"unsupported source: {source}")
    if not dry_run and not insert:
        raise RuntimeError("Use --dry-run or --insert")
    catalog_map = load_catalog_key_map()
    all_brand_rows: list[dict[str, Any]] = []
    all_market_rows: list[dict[str, Any]] = []
    measure_stats = {}
    ubist_base = load_ubist_base_frame(max_rows=max_rows, ml=ml) if source == "ubist" else None
    iqvia_base = load_iqvia_base_frame(max_rows=max_rows) if source == "iqvia_nsa" else None
    if insert:
        ensure_json_columns("mart_general_brand_metric", ("dimension_data", "dimension_channel_data"))
        delete_source_rows("mart_general_brand_metric", source)
        delete_source_rows("mart_general_market_metric", source)
    for measure in MEASURES_BY_SOURCE[source]:
        frame = ubist_measure_frame(ubist_base, measure) if source == "ubist" else iqvia_measure_frame(iqvia_base, measure)
        input_rows = 0
        brand_count = 0
        market_count = 0
        for atc4_code, chunk in iter_atc4_chunks(frame, limit_atc4):
            input_rows += int(len(chunk))
            brand_rows = build_brand_rows(source, measure, chunk, catalog_map)
            market_rows = build_market_rows(source, measure, brand_rows)
            brand_count += len(brand_rows)
            market_count += len(market_rows)
            if dry_run:
                all_brand_rows.extend(brand_rows)
                all_market_rows.extend(market_rows)
            if insert:
                insert_rows("mart_general_brand_metric", GENERAL_BRAND_INSERT_COLUMNS, brand_rows)
                insert_rows("mart_general_market_metric", GENERAL_MARKET_INSERT_COLUMNS, market_rows)
            LOGGER.info(
                "[%s/%s/%s] input=%s brand_rows=%s market_rows=%s",
                source,
                measure,
                atc4_code,
                f"{len(chunk):,}",
                f"{len(brand_rows):,}",
                f"{len(market_rows):,}",
            )
            del chunk, brand_rows, market_rows
        measure_stats[measure] = {"input_rows": input_rows, "brand_rows": brand_count, "market_rows": market_count}
        LOGGER.info("[%s/%s] input=%s brand_rows=%s market_rows=%s", source, measure, f"{input_rows:,}", f"{brand_count:,}", f"{market_count:,}")
    if dry_run:
        write_jsonl(general_brand_jsonl_path(source, output_dir), all_brand_rows)
        write_jsonl(general_market_jsonl_path(source, output_dir), all_market_rows)
    stats = {
        "source": source,
        "brand_rows": sum(item["brand_rows"] for item in measure_stats.values()),
        "market_rows": sum(item["market_rows"] for item in measure_stats.values()),
        "measures": measure_stats,
    }
    return all_brand_rows, all_market_rows, stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=ALLOWED_SOURCES)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--insert", action="store_true")
    parser.add_argument("--limit-atc4", type=int, default=None)
    parser.add_argument("--max-rows", type=int, default=None, help="Optional raw-row limit for fast validation only")
    parser.add_argument("--output-dir", type=Path, default=DRY_RUN_DIR)
    parser.add_argument("--ml", help="Optional Layer 2 ml_id filter for fast UBIST validation")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.all and args.source:
        raise SystemExit("--all and --source are mutually exclusive")
    if not args.all and not args.source:
        raise SystemExit("Provide --source SOURCE or --all")
    sources = list(ALLOWED_SOURCES) if args.all else [args.source]
    for source in sources:
        brand_rows, market_rows, stats = compute_general(
            source=source,
            dry_run=args.dry_run,
            insert=args.insert,
            limit_atc4=args.limit_atc4,
            max_rows=args.max_rows,
            output_dir=args.output_dir,
            ml=args.ml,
        )
        print(f"\n=== {source} general v3.1 ===")
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
