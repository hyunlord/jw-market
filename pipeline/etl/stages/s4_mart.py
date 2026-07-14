from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pymysql

from pipeline.etl.lib.ops_utils import find_project_root, first_existing


STAGE = "s4 mart"

PROJECT_ROOT = find_project_root(Path(__file__).resolve())

GENERAL_BRAND_DDL = """
CREATE TABLE IF NOT EXISTS mart_general_brand_metric (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  brand_key VARCHAR(255) NOT NULL,
  brand_name VARCHAR(255) NOT NULL,
  atc4_code VARCHAR(16) NOT NULL,
  atc4_desc VARCHAR(255) NULL,
  source VARCHAR(16) NOT NULL,
  measure VARCHAR(32) NOT NULL,
  unit_label VARCHAR(32) NOT NULL,
  metric_history JSON NOT NULL,
  extended_metric_history JSON NOT NULL,
  channel_data JSON NOT NULL,
  specialty_data JSON NOT NULL,
  channel_specialty_matrix JSON NULL,
  audit_code_matrix LONGTEXT NULL CHECK (audit_code_matrix IS NULL OR JSON_VALID(audit_code_matrix)),
  dimension_data JSON NOT NULL,
  dimension_channel_data JSON NOT NULL,
  by_dimension JSON NOT NULL,
  raw_value_history JSON NOT NULL,
  payload JSON NULL,
  computation_version VARCHAR(16) DEFAULT 'v3',
  computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_general_brand (brand_key, atc4_code, source, measure),
  INDEX idx_general_brand_key (brand_key, source, measure),
  INDEX idx_general_brand_name (brand_name, measure),
  INDEX idx_general_brand_atc4 (atc4_code, source, measure),
  INDEX idx_general_brand_source (source, measure),
  INDEX idx_general_atc_universe (source, atc4_code, atc4_desc)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

GENERAL_MARKET_DDL = """
CREATE TABLE IF NOT EXISTS mart_general_market_metric (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  atc4_code VARCHAR(16) NOT NULL,
  atc4_desc VARCHAR(255) NULL,
  source VARCHAR(16) NOT NULL,
  measure VARCHAR(32) NOT NULL,
  unit_label VARCHAR(32) NOT NULL,
  market_size_series JSON NOT NULL,
  hhi_series JSON NOT NULL,
  brand_ranking JSON NOT NULL,
  company_ranking_stacked JSON NOT NULL,
  company_concentration_trend JSON NOT NULL,
  ei_ms_matrix JSON NOT NULL,
  growth_contribution_ms_matrix JSON NOT NULL,
  growth_contribution JSON NOT NULL,
  analysis_levels JSON NOT NULL,
  level_top5_trend JSON NOT NULL,
  target_customer_competition JSON NOT NULL,
  payload JSON NULL,
  computation_version VARCHAR(16) DEFAULT 'v3',
  computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_general_market (atc4_code, source, measure),
  INDEX idx_general_market_atc4 (atc4_code),
  INDEX idx_general_market_source (source, measure)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""


def _env() -> dict[str, str]:
    from pipeline.etl.io.mart.layer3_compute_general_v3 import load_env

    env_path = first_existing(PROJECT_ROOT / "pipeline" / "docker" / ".env", PROJECT_ROOT / "docker" / ".env")
    env = load_env(env_path)
    for key, value in os.environ.items():
        if key.startswith("MARIADB_") or key == "HOST_PORT":
            env[key] = value
    return env


def _admin_connect(env: dict[str, str]) -> pymysql.connections.Connection:
    password = env.get("MARIADB_ROOT_PASSWORD") or env.get("MARIADB_PASSWORD")
    user = "root" if env.get("MARIADB_ROOT_PASSWORD") else env.get("MARIADB_USER", "jwapp")
    if not password:
        raise RuntimeError("MARIADB_ROOT_PASSWORD/MARIADB_PASSWORD is missing")
    return pymysql.connect(
        host=env.get("MARIADB_HOST", "127.0.0.1"),
        port=int(env.get("MARIADB_PORT") or env.get("HOST_PORT", "3307")),
        user=user,
        password=password,
        charset="utf8mb4",
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


def _ensure_isolated_schema(target_db: str, source_db: str) -> None:
    if target_db in {"jw_mart", source_db}:
        raise ValueError(f"refusing to write s4 mart into operating/source DB: {target_db}")
    env = _env()
    conn = _admin_connect(env)
    try:
        with conn.cursor() as cur:
            cur.execute(f"CREATE DATABASE IF NOT EXISTS `{target_db}` DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci")
            cur.execute(f"USE `{target_db}`")
            cur.execute("DROP TABLE IF EXISTS mart_general_brand_metric")
            cur.execute("DROP TABLE IF EXISTS mart_general_market_metric")
            cur.execute(GENERAL_BRAND_DDL)
            cur.execute(GENERAL_MARKET_DDL)
    finally:
        conn.close()


def _configure_mart_env(target_db: str, source_db: str) -> None:
    env = _env()
    password = env.get("MARIADB_ROOT_PASSWORD") or env.get("MARIADB_PASSWORD")
    user = "root" if env.get("MARIADB_ROOT_PASSWORD") else env.get("MARIADB_USER", "jwapp")
    if not password:
        raise RuntimeError("MARIADB_ROOT_PASSWORD/MARIADB_PASSWORD is missing")
    os.environ["MARIADB_DATABASE"] = target_db
    os.environ["MARIADB_SOURCE_DATABASE"] = source_db
    os.environ["MARIADB_USER"] = user
    os.environ["MARIADB_PASSWORD"] = password
    os.environ.setdefault("MARIADB_HOST", env.get("MARIADB_HOST", "127.0.0.1"))
    os.environ.setdefault("HOST_PORT", env.get("HOST_PORT", "3307"))
    if env.get("MARIADB_PORT"):
        os.environ.setdefault("MARIADB_PORT", env["MARIADB_PORT"])


def run(params: dict[str, Any]) -> int:
    target_db = str(params.get("target_db") or "").strip()
    source_db = str(params.get("source_db") or "jw_mart").strip()
    if not target_db:
        print(f"[{STAGE}] 실패: --target-db is required for isolated mart writes")
        return 2
    try:
        enriched_dir = params.get("enriched_dir") or params.get("target_dir")
        if enriched_dir:
            os.environ["S4_ENRICHED_DIR"] = str(enriched_dir)
        if params.get("catalog_root"):
            os.environ["S4_CATALOG_DIR"] = str(params["catalog_root"])
        os.environ["S4_INPUT_MODE"] = str(params.get("input_mode") or "raw")
        if params.get("iqvia_nsa_dir"):
            os.environ["S4_IQVIA_NSA_DIR"] = str(params["iqvia_nsa_dir"])
        if params.get("ubist_dir"):
            os.environ["S4_UBIST_DIR"] = str(params["ubist_dir"])
        _ensure_isolated_schema(target_db, source_db)
        _configure_mart_env(target_db, source_db)
        from pipeline.etl.io.mart.layer3_compute_general_v3 import compute_general

        stats: list[dict[str, Any]] = []
        for source in ("ubist", "iqvia_nsa"):
            _, _, source_stats = compute_general(
                source,
                insert=True,
                limit_atc4=params.get("limit_atc4"),
                max_rows=params.get("max_rows"),
            )
            stats.append(source_stats)
            print(
                f"[{STAGE}] {source}: brand_rows={source_stats['brand_rows']} "
                f"market_rows={source_stats['market_rows']} measures={source_stats['measures']}"
            )
    except Exception as exc:
        print(f"[{STAGE}] 실패: {exc}")
        return 1
    print(f"[{STAGE}] 완료 target_db={target_db} source_db={source_db}")
    return 0
