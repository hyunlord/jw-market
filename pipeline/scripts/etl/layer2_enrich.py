#!/usr/bin/env python3
"""Build Layer 2 enriched fact parquet from strategic_product and Layer 1 raw data."""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import duckdb
import pandas as pd
import pymysql
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ops_utils import configure_logging, find_project_root, first_existing, retry  # noqa: E402
from storage import is_minio_backend, upload_local_to_minio  # noqa: E402,F401
from layer2_normalize import (  # noqa: E402
    canonical_iqvia_channel,
    clean_scalar,
    extract_bracket_code,
    load_customer_dictionary,
    map_channel_ubist,
    map_specialty_ubist,
    normalize_atc,
    normalize_brand,
    normalize_product_title,
)


LOGGER = configure_logging(__name__)
REPO_ROOT = find_project_root(Path(__file__).resolve())
UBIST_DIR = first_existing(REPO_ROOT / "output" / "ubist", REPO_ROOT / "parquet" / "ubist")
UBIST_GLOB = str(UBIST_DIR / "year=*" / "month=*" / "data.parquet")
CATALOG_OUTPUT_DIR = first_existing(REPO_ROOT / "output" / "catalog", REPO_ROOT / "parquet")
ENRICHED_DIR = REPO_ROOT / "output" / "enriched"
AUDIT_DIR = REPO_ROOT / "audit" / "phase16d_layer2"
KST = ZoneInfo("Asia/Seoul")

ENRICHED_COLUMNS = [
    "ml_id",
    "product_id",
    "source",
    "period_yyyymm",
    "raw_rx_amt",
    "raw_rx_cnt",
    "raw_rx_qty",
    "canonical_value",
    "channel",
    "specialty",
    "match_method",
    "match_confidence",
    "source_table",
    "source_row_id",
    "ingested_at",
]


@dataclass
class EnrichResult:
    ml_id: str
    rows: int
    matched_products: int
    total_products: int
    sources: dict[str, int]
    skipped_sources: list[str]

    @property
    def product_match_rate(self) -> float:
        if self.total_products == 0:
            return 0.0
        return self.matched_products / self.total_products


@dataclass
class ProductIndex:
    exact: dict[str, list[dict[str, Any]]]
    brand: dict[str, list[dict[str, Any]]]


def now_iso() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


@retry((pymysql.err.OperationalError, pymysql.err.InterfaceError), logger=LOGGER)
def mariadb_connect(cursorclass: type | None = None) -> pymysql.connections.Connection:
    env_path = first_existing(REPO_ROOT / "pipeline" / "docker" / ".env", REPO_ROOT / "docker" / ".env")
    if not env_path.exists():
        raise FileNotFoundError(f"Missing MariaDB env file: {env_path}")
    env = load_env(env_path)
    kwargs: dict[str, Any] = {
        "host": "127.0.0.1",
        "port": int(env.get("HOST_PORT", "3307")),
        "user": env.get("MARIADB_USER", "jwapp"),
        "password": env["MARIADB_PASSWORD"],
        "database": env.get("MARIADB_DATABASE", "jw_mart"),
        "charset": "utf8mb4",
        "autocommit": False,
    }
    if cursorclass is not None:
        kwargs["cursorclass"] = cursorclass
    return pymysql.connect(**kwargs)


def load_market_metadata() -> dict[str, Any]:
    path = REPO_ROOT / "catalog" / "market_metadata.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Missing market metadata: {path}")
    with path.open(encoding="utf-8") as fp:
        return yaml.safe_load(fp) or {}


def load_ml_market() -> pd.DataFrame:
    path = CATALOG_OUTPUT_DIR / "ml_market" / "ml_market.parquet"
    if not path.exists():
        raise FileNotFoundError(f"Missing ml_market parquet: {path}")
    return pd.read_parquet(path)


def load_strategic_product(ml_id: str) -> pd.DataFrame:
    path = CATALOG_OUTPUT_DIR / "strategic_product" / "strategic_product.parquet"
    if not path.exists():
        raise FileNotFoundError(f"Missing strategic_product parquet: {path}")
    sp = pd.read_parquet(path)
    products = sp[sp["ml_id"] == ml_id].copy()
    products["ubist_product_title"] = products["name"].fillna(products["merge_name"]).fillna("")
    products["iqvia_product_title"] = products["merge_name"].fillna(products["name"]).fillna("")
    products["ubist_product_key"] = products["ubist_product_title"].map(normalize_product_title)
    products["iqvia_product_key"] = products["iqvia_product_title"].map(normalize_product_title)
    products["product_key"] = products["ubist_product_key"]
    products["brand_key"] = products["iqvia_product_title"].map(normalize_brand)
    products["strength_bracket_code"] = products["strength_pack"].map(extract_bracket_code)
    products = products[(products["ubist_product_key"] != "") | (products["iqvia_product_key"] != "")].copy()
    return products


def build_product_index(products: pd.DataFrame) -> ProductIndex:
    exact: dict[str, list[dict[str, Any]]] = {}
    brand: dict[str, list[dict[str, Any]]] = {}
    for record in products.to_dict("records"):
        product_key = clean_scalar(record.get("iqvia_product_key") or record.get("product_key"))
        brand_key = clean_scalar(record.get("brand_key"))
        if product_key:
            exact.setdefault(product_key, []).append(record)
        if brand_key:
            brand.setdefault(brand_key, []).append(record)
    return ProductIndex(exact=exact, brand=brand)


def target_iqvia_channels(ml_row: pd.Series) -> list[str]:
    targets: list[str] = []
    for col in ("target_iqvia_1", "target_iqvia_2", "target_iqvia_3"):
        value = clean_scalar(ml_row.get(col))
        if value:
            targets.append(value.upper())
    return targets


def ml_data_source(ml_row: pd.Series) -> str:
    value = clean_scalar(ml_row.get("data_source")).lower()
    if value in {"ubist", "iqvia", "both"}:
        return value
    return "iqvia"


def catalog_atc_codes(metadata: dict[str, Any], ml_id: str) -> list[str]:
    market = (metadata.get("markets") or {}).get(ml_id, {})
    return [clean_scalar(v).upper() for v in market.get("atc_codes", []) if clean_scalar(v)]


def sql_literal_list(values: Iterable[str]) -> str:
    cleaned = [v for v in values if v]
    if not cleaned:
        return "('')"
    return "(" + ", ".join("'" + v.replace("'", "''") + "'" for v in cleaned) + ")"


def duckdb_product_key_expr(expr: str) -> str:
    return (
        "lower(regexp_replace("
        f"replace(replace(replace(cast({expr} as varchar), '㎎', 'mg'), 'ＭＧ', 'mg'), ' ', ''), "
        "'\\\\s+', '', 'g'))"
    )


def duckdb_case_map(
    expr: str,
    mapping: dict[str, str],
    default: str = "Unknown",
    contains: bool = False,
) -> str:
    clauses = []
    for raw, canonical in mapping.items():
        raw_sql = str(raw).replace("'", "''")
        val_sql = str(canonical).replace("'", "''")
        if contains:
            clauses.append(f"WHEN cast({expr} as varchar) LIKE '%{raw_sql}%' THEN '{val_sql}'")
        else:
            clauses.append(f"WHEN cast({expr} as varchar) = '{raw_sql}' THEN '{val_sql}'")
    return f"CASE {' '.join(clauses)} ELSE '{default}' END"


def register_products(con: duckdb.DuckDBPyConnection, products: pd.DataFrame) -> None:
    rows: list[dict[str, Any]] = []
    for record in products.to_dict("records"):
        key_pairs = {
            clean_scalar(record.get("ubist_product_key")): clean_scalar(record.get("ubist_product_title")),
            clean_scalar(record.get("iqvia_product_key")): clean_scalar(record.get("iqvia_product_title")),
        }
        for key, title in key_pairs.items():
            if not key:
                continue
            rows.append(
                {
                    "product_id": record["product_id"],
                    "ml_id": record["ml_id"],
                    "product_title": title,
                    "product_key": key,
                    "brand_key": record.get("brand_key"),
                    "strength_bracket_code": record.get("strength_bracket_code"),
                }
            )
    bridge = pd.DataFrame(rows).drop_duplicates()
    con.register("product_bridge", bridge)


def ubist_join_sql(customer_dict: dict[str, Any]) -> str:
    product_key = duckdb_product_key_expr("u.제품")
    channel_case = duckdb_case_map("u.종별", customer_dict.get("ubist_channel", {}), default="Unknown")
    specialty_case = duckdb_case_map(
        "u.진료과",
        customer_dict.get("ubist_specialty", {}),
        default="Unknown",
        contains=True,
    )
    ingested = now_iso().replace("'", "''")
    return f"""
        SELECT DISTINCT
          p.ml_id AS ml_id,
          p.product_id AS product_id,
          'ubist' AS source,
          u.period_yyyymm AS period_yyyymm,
          try_cast(u.rx_amt AS DOUBLE) AS raw_rx_amt,
          try_cast(u.rx_cnt AS DOUBLE) AS raw_rx_cnt,
          try_cast(u.rx_qty AS DOUBLE) AS raw_rx_qty,
          try_cast(u.rx_amt AS DOUBLE) AS canonical_value,
          {channel_case} AS channel,
          {specialty_case} AS specialty,
          'product_name_exact' AS match_method,
          'high' AS match_confidence,
          'ubist_parquet' AS source_table,
          concat(
            'ubist::',
            coalesce(cast(u.source_file AS varchar), ''),
            '::',
            coalesce(cast(u.source_sheet AS varchar), ''),
            '::',
            coalesce(cast(u.source_row_no AS varchar), ''),
            '::',
            coalesce(cast(u.period_yyyymm AS varchar), ''),
            '::',
            coalesce(cast(u.약품코드 AS varchar), '')
          ) AS source_row_id,
          '{ingested}' AS ingested_at
        FROM read_parquet('{UBIST_GLOB}') AS u
        JOIN product_bridge AS p
          ON {product_key} = p.product_key
    """


def summarize_ubist_dry_run(
    con: duckdb.DuckDBPyConnection, products: pd.DataFrame, customer_dict: dict[str, Any]
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, int]]:
    sql = ubist_join_sql(customer_dict)
    counts = con.execute(
        f"""
        SELECT
          COUNT(*) AS rows,
          COUNT(DISTINCT product_id) AS matched_products
        FROM ({sql})
        """
    ).fetchone()
    sample = con.execute(f"SELECT * FROM ({sql}) LIMIT 10").df()
    matched = con.execute(f"SELECT DISTINCT product_id FROM ({sql})").df()
    matched_ids = set(matched["product_id"]) if not matched.empty else set()
    unmatched = products.loc[~products["product_id"].isin(matched_ids), [
        "ml_id",
        "product_id",
        "name",
        "merge_name",
        "strength_pack",
        "ubist_product_key",
        "iqvia_product_key",
        "brand_key",
    ]].copy()
    return sample, unmatched, {"rows": int(counts[0] or 0), "matched_products": int(counts[1] or 0)}


def write_ubist_ml(
    ml_id: str,
    products: pd.DataFrame,
    customer_dict: dict[str, Any],
    output_path: Path,
) -> tuple[int, int]:
    con = duckdb.connect()
    register_products(con, products)
    sql = ubist_join_sql(customer_dict)
    tmp = output_path.with_suffix(".tmp.parquet")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if tmp.exists():
        tmp.unlink()
    con.execute(f"COPY ({sql}) TO '{tmp}' (FORMAT PARQUET, COMPRESSION SNAPPY)")
    tmp.replace(output_path)
    stats = con.execute(f"SELECT COUNT(*) AS rows, COUNT(DISTINCT product_id) AS products FROM read_parquet('{output_path}')").fetchone()
    con.close()
    return int(stats[0] or 0), int(stats[1] or 0)


def parse_payload(payload: str | bytes | dict[str, Any]) -> dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    return json.loads(payload)


def nsa_candidate_sql(targets: list[str]) -> tuple[str, list[str]]:
    if not targets:
        return "SELECT id, source_file, sheet_name, source_row_no, audit_code, period_yyyy, period_quarter, period_label, payload FROM iqvia_nsa_quarterly_raw", []
    clauses = []
    params = []
    for target in targets:
        clauses.append("audit_code LIKE %s")
        params.append(f"{target}%")
    where = " OR ".join(clauses)
    return (
        "SELECT id, source_file, sheet_name, source_row_no, audit_code, period_yyyy, "
        f"period_quarter, period_label, payload FROM iqvia_nsa_quarterly_raw WHERE {where}",
        params,
    )


def product_match_from_iqvia(index: ProductIndex, static: dict[str, Any]) -> list[tuple[dict[str, Any], str, str]]:
    product_name = clean_scalar(static.get("PRODUCT NAME KOR") or static.get("PRODUCT NAME"))
    pack_desc = clean_scalar(static.get("PACK DESC") or static.get("PACK DESCRIPTION"))
    name_key = normalize_product_title(product_name)
    full_key = normalize_product_title(f"{product_name} {pack_desc}")
    product_matches: list[tuple[dict[str, Any], str, str]] = []
    seen: set[str] = set()

    for key in {name_key, full_key}:
        if not key:
            continue
        for product in index.exact.get(key, []):
            product_id = str(product["product_id"])
            if product_id not in seen:
                product_matches.append((product, "product_name_pack", "high"))
                seen.add(product_id)

    for key, products in index.exact.items():
        if not key or len(key) < 3:
            continue
        if (key in full_key or full_key in key) and key not in {name_key, full_key}:
            for product in products:
                product_id = str(product["product_id"])
                if product_id not in seen:
                    product_matches.append((product, "product_name_pack_contains", "high"))
                    seen.add(product_id)

    for key, products in index.brand.items():
        if not key or len(key) < 2:
            continue
        if key == name_key or key in name_key or key in full_key:
            for product in products:
                product_id = str(product["product_id"])
                if product_id not in seen:
                    product_matches.append((product, "brand_only", "medium"))
                    seen.add(product_id)
    return product_matches


def metric_value(payload: dict[str, Any], *names: str) -> Any:
    values = payload.get("period_values") or {}
    for name in names:
        if name in values:
            return values[name]
    return None


def append_iqvia_rows_for_ml(
    ml_id: str,
    products: pd.DataFrame,
    ml_row: pd.Series,
    atc_codes: list[str],
    source: str,
) -> pd.DataFrame:
    if source == "csd":
        return pd.DataFrame(columns=ENRICHED_COLUMNS)

    product_index = build_product_index(products)
    conn = mariadb_connect(cursorclass=pymysql.cursors.SSCursor)
    rows: list[dict[str, Any]] = []
    if source == "nsa":
        sql, params = nsa_candidate_sql(target_iqvia_channels(ml_row))
    elif source == "chso":
        sql, params = (
            "SELECT id, source_file, sheet_name, source_row_no, period_yyyymm, payload "
            "FROM iqvia_chso_monthly_raw",
            [],
        )
    else:
        raise ValueError(source)

    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            for raw in cur:
                if source == "nsa":
                    row_id, source_file, sheet_name, source_row_no, audit_code, year, quarter, period_label, payload_raw = raw
                    period = f"{int(year):04d}-Q{int(quarter)}" if year and quarter else clean_scalar(period_label)
                    channel = canonical_iqvia_channel(audit_code)
                    source_table = "nsa_mariadb"
                    source_row_id = f"nsa::{row_id}"
                    metric_names = ("Values LC",)
                    qty_names = ("Units", "Dosage Units")
                    cnt_names = ("Counting Units",)
                else:
                    row_id, source_file, sheet_name, source_row_no, period, payload_raw = raw
                    channel = "Sell_Out"
                    source_table = "chso_mariadb"
                    source_row_id = f"chso::{row_id}"
                    metric_names = ("VALUES LC SI PRICE",)
                    qty_names = ("UNITS",)
                    cnt_names = ()

                payload = parse_payload(payload_raw)
                static = payload.get("static") or {}
                raw_atc = clean_scalar(static.get("ATC 4 CODE") or static.get("ATC 4"))
                raw_atc_code = raw_atc.split("_", 1)[0].upper() if raw_atc else ""
                if atc_codes and raw_atc_code and raw_atc_code not in atc_codes:
                    continue
                matches = product_match_from_iqvia(product_index, static)
                if not matches:
                    continue

                amt = metric_value(payload, *metric_names)
                qty = metric_value(payload, *qty_names)
                cnt = metric_value(payload, *cnt_names)
                for product, method, confidence in matches:
                    rows.append(
                        {
                            "ml_id": ml_id,
                            "product_id": product["product_id"],
                            "source": source,
                            "period_yyyymm": period,
                            "raw_rx_amt": amt,
                            "raw_rx_cnt": cnt,
                            "raw_rx_qty": qty,
                            "canonical_value": amt,
                            "channel": channel,
                            "specialty": "",
                            "match_method": method,
                            "match_confidence": confidence,
                            "source_table": source_table,
                            "source_row_id": source_row_id,
                            "ingested_at": now_iso(),
                        }
                    )
    finally:
        conn.close()
    return pd.DataFrame(rows, columns=ENRICHED_COLUMNS)


def merge_parquet_sources(output_path: Path, frames: list[pd.DataFrame]) -> tuple[int, int]:
    existing: list[pd.DataFrame] = []
    if output_path.exists():
        existing.append(pd.read_parquet(output_path))
    for frame in frames:
        if not frame.empty:
            existing.append(frame)
    if existing:
        df = pd.concat(existing, ignore_index=True)
    else:
        df = pd.DataFrame(columns=ENRICHED_COLUMNS)
    for col in ENRICHED_COLUMNS:
        if col not in df.columns:
            df[col] = None
    df = df.reindex(columns=ENRICHED_COLUMNS)
    for col in ["raw_rx_amt", "raw_rx_cnt", "raw_rx_qty", "canonical_value"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in [
        "ml_id",
        "product_id",
        "source",
        "period_yyyymm",
        "channel",
        "specialty",
        "match_method",
        "match_confidence",
        "source_table",
        "source_row_id",
        "ingested_at",
    ]:
        df[col] = df[col].fillna("").astype(str)
    tmp = output_path.with_suffix(".tmp.parquet")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(tmp, engine="pyarrow", compression="snappy", index=False)
    tmp.replace(output_path)
    return int(len(df)), int(df["product_id"].nunique() if not df.empty else 0)


def enrich_ml(
    ml_id: str,
    dry_run: bool,
    audit_dir: Path,
    output_dir: Path = ENRICHED_DIR,
) -> EnrichResult:
    audit_dir.mkdir(parents=True, exist_ok=True)
    customer_dict = load_customer_dictionary()
    metadata = load_market_metadata()
    ml_market = load_ml_market()
    ml_rows = ml_market[ml_market["ml_id"] == ml_id]
    if ml_rows.empty:
        raise ValueError(f"Unknown ml_id: {ml_id}")
    ml_row = ml_rows.iloc[0]
    data_source = ml_data_source(ml_row)
    products = load_strategic_product(ml_id)
    atc_codes = catalog_atc_codes(metadata, ml_id)
    output_path = output_dir / f"ml_id={ml_id}" / "data.parquet"
    sources: dict[str, int] = {}
    skipped_sources: list[str] = []
    matched_products = 0
    total_rows = 0

    if dry_run:
        lines = [
            f"# Layer 2 Dry Run — {ml_id}",
            "",
            f"- generated_at: {now_iso()}",
            f"- data_source: {data_source}",
            f"- strategic_product rows: {len(products):,}",
            f"- catalog ATC: {catalog_atc_codes(metadata, ml_id)}",
            f"- normalized ATC: {[normalize_atc(v) for v in catalog_atc_codes(metadata, ml_id)]}",
            "",
        ]

        if data_source in {"ubist", "both"}:
            con = duckdb.connect()
            register_products(con, products)
            sample, unmatched, stats = summarize_ubist_dry_run(con, products, customer_dict)
            con.close()
            total_rows += stats["rows"]
            matched_products = max(matched_products, stats["matched_products"])
            sources["ubist"] = stats["rows"]
            lines.extend(
                [
                    "## UBIST Product Bridge",
                    "",
                    "- match_rule: normalized strategic_product.name OR merge_name == normalized UBIST `제품`",
                    f"- matched rows: {stats['rows']:,}",
                    f"- matched products: {stats['matched_products']:,} / {len(products):,} ({stats['matched_products'] / len(products) * 100 if len(products) else 0:.2f}%)",
                    f"- unmatched products: {len(unmatched):,}",
                    "",
                    "### Sample Rows",
                    "",
                    dataframe_to_markdown(sample),
                    "",
                    "### Unmatched Products (first 30)",
                    "",
                    dataframe_to_markdown(unmatched, max_rows=30),
                    "",
                ]
            )

        if data_source in {"iqvia", "both"}:
            lines.extend(
                [
                    "## IQVIA Product Bridge",
                    "",
                    "- NSA/CHSO matching is implemented by PRODUCT NAME KOR + PACK DESC product title matching.",
                    "- CSD is skipped for Layer 2 product fact because payload rows are call/rank supplemental facts, not product sales rows.",
                    "",
                ]
            )
            skipped_sources.append("csd")

        out_name = f"dry_run_{ml_id}.md"
        (audit_dir / out_name).write_text("\n".join(lines), encoding="utf-8")
        return EnrichResult(ml_id, total_rows, matched_products, len(products), sources, skipped_sources)

    if output_path.exists():
        output_path.unlink()
    if data_source in {"ubist", "both"}:
        if not UBIST_DIR.exists():
            raise FileNotFoundError(f"Missing UBIST parquet directory: {UBIST_DIR}")
        rows, prod_count = write_ubist_ml(ml_id, products, customer_dict, output_path)
        sources["ubist"] = rows
        total_rows += rows
        matched_products = max(matched_products, prod_count)

    frames: list[pd.DataFrame] = []
    if data_source in {"iqvia", "both"}:
        for source in ("nsa", "chso"):
            frame = append_iqvia_rows_for_ml(ml_id, products, ml_row, atc_codes, source)
            sources[source] = len(frame)
            frames.append(frame)
        skipped_sources.append("csd")
    if frames:
        total_rows, prod_count = merge_parquet_sources(output_path, frames)
        matched_products = max(matched_products, prod_count)
    elif not output_path.exists():
        merge_parquet_sources(output_path, [])
    return EnrichResult(ml_id, total_rows, matched_products, len(products), sources, skipped_sources)


def all_ml_ids() -> list[str]:
    ml = load_ml_market()
    return sorted(ml["ml_id"].tolist())


def write_loading_csv(results: list[EnrichResult], audit_dir: Path) -> None:
    with (audit_dir / "enriched_summary.csv").open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(
            fp,
            fieldnames=[
                "ml_id",
                "rows",
                "matched_products",
                "total_products",
                "product_match_rate",
                "sources",
                "skipped_sources",
            ],
        )
        writer.writeheader()
        for result in results:
            writer.writerow(
                {
                    "ml_id": result.ml_id,
                    "rows": result.rows,
                    "matched_products": result.matched_products,
                    "total_products": result.total_products,
                    "product_match_rate": f"{result.product_match_rate:.6f}",
                    "sources": json.dumps(result.sources, ensure_ascii=False),
                    "skipped_sources": ";".join(result.skipped_sources),
                }
            )


def dataframe_to_markdown(df: pd.DataFrame, max_rows: int | None = None) -> str:
    if df.empty:
        return "(none)"
    view = df.head(max_rows).copy() if max_rows else df.copy()
    view = view.fillna("")
    columns = [str(c) for c in view.columns]
    lines = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join(["---"] * len(columns)) + " |",
    ]
    for _, row in view.iterrows():
        values = [str(row[col]).replace("\n", " ").replace("|", "\\|") for col in view.columns]
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ml", help="Single ml_id to enrich")
    group.add_argument("--all", action="store_true", help="Enrich all ml markets")
    parser.add_argument("--dry-run", action="store_true", help="Analyze matching without writing enriched parquet")
    parser.add_argument("--audit-dir", default=str(AUDIT_DIR))
    parser.add_argument("--output-dir", default=str(ENRICHED_DIR))
    parser.add_argument("--truncate", action="store_true", help="Remove output directory before --all load")
    return parser.parse_args()


def main() -> int:
    try:
        args = parse_args()
        audit_dir = Path(args.audit_dir)
        output_dir = Path(args.output_dir)
        audit_dir.mkdir(parents=True, exist_ok=True)

        if args.truncate and output_dir.exists() and not args.dry_run:
            shutil.rmtree(output_dir)

        targets = [args.ml] if args.ml else all_ml_ids()
        results: list[EnrichResult] = []
        for ml_id in targets:
            LOGGER.info("enriching %s dry_run=%s", ml_id, args.dry_run)
            result = enrich_ml(ml_id, dry_run=args.dry_run, audit_dir=audit_dir, output_dir=output_dir)
            results.append(result)
            LOGGER.info(
                "rows=%s matched_products=%s/%s (%s) sources=%s",
                f"{result.rows:,}",
                f"{result.matched_products:,}",
                f"{result.total_products:,}",
                f"{result.product_match_rate:.2%}",
                result.sources,
            )

        write_loading_csv(results, audit_dir)
        return 0
    except Exception:
        LOGGER.exception("Layer 2 enrichment failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
