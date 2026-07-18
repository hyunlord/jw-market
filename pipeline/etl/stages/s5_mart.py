from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pymysql

from pipeline.etl.isolation import validate_mart_schema_pair
from pipeline.etl.lib.ops_utils import find_project_root, first_existing

STAGE = "s5 mart"
PROJECT_ROOT = find_project_root(Path(__file__).resolve())

STRATEGIC_ML_BRAND_DDL = """
CREATE TABLE IF NOT EXISTS mart_strategic_ml_brand_metric (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  ml_id VARCHAR(32) NOT NULL,
  brand_id VARCHAR(255) NOT NULL,
  brand_key VARCHAR(255) NOT NULL,
  brand_name VARCHAR(255) NOT NULL,
  source VARCHAR(16) NOT NULL,
  measure VARCHAR(32) NOT NULL,
  is_jw BOOLEAN NULL,
  unit_label VARCHAR(32) NOT NULL,
  metric_history JSON NOT NULL,
  extended_metric_history JSON NOT NULL,
  channel_data JSON NOT NULL,
  specialty_data JSON NOT NULL,
  dimension_data JSON NOT NULL,
  dimension_channel_data JSON NOT NULL,
  dimension_specialty_data JSON NULL,
  by_dimension JSON NOT NULL,
  raw_value_history JSON NOT NULL,
  ubist_channel_by_display JSON NULL,
  ubist_channel_by_code JSON NULL,
  overlay_data JSON NULL,
  payload JSON NULL,
  computation_version VARCHAR(16) DEFAULT 'v3',
  computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_ml_brand (ml_id, brand_id, source, measure)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

STRATEGIC_ML_MARKET_DDL = """
CREATE TABLE IF NOT EXISTS mart_strategic_ml_market_metric (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  ml_id VARCHAR(32) NOT NULL,
  ml_name VARCHAR(255) NULL,
  source VARCHAR(16) NOT NULL,
  measure VARCHAR(32) NOT NULL,
  unit_label VARCHAR(32) NOT NULL,
  market_size_series JSON NOT NULL,
  hhi_series_5y JSON NOT NULL,
  brand_ranking_stacked JSON NOT NULL,
  company_ranking_stacked JSON NOT NULL,
  company_concentration_trend JSON NOT NULL,
  ei_ms_matrix JSON NOT NULL,
  growth_contribution_ms_matrix JSON NOT NULL,
  growth_contribution JSON NOT NULL,
  analysis_levels JSON NULL,
  level_top5_trend JSON NULL,
  target_customer_competition JSON NULL,
  payload JSON NULL,
  computation_version VARCHAR(16) DEFAULT 'v3',
  computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_ml_market (ml_id, source, measure)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

STRATEGIC_CD_BRAND_DDL = """
CREATE TABLE IF NOT EXISTS mart_strategic_cd_brand_metric (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  cd_market_id VARCHAR(32) NOT NULL,
  cd_brand_id VARCHAR(255) NOT NULL,
  brand_key VARCHAR(255) NOT NULL,
  brand_name VARCHAR(255) NOT NULL,
  source VARCHAR(16) NOT NULL,
  measure VARCHAR(32) NOT NULL,
  is_jw BOOLEAN NULL,
  unit_label VARCHAR(32) NOT NULL,
  metric_history JSON NOT NULL,
  extended_metric_history JSON NOT NULL,
  channel_data JSON NOT NULL,
  specialty_data JSON NOT NULL,
  dimension_data JSON NOT NULL,
  dimension_channel_data JSON NOT NULL,
  by_dimension JSON NOT NULL,
  raw_value_history JSON NOT NULL,
  ubist_channel_by_display JSON NULL,
  ubist_channel_by_code JSON NULL,
  cd_overlay JSON NULL,
  overlay_data JSON NULL,
  payload JSON NULL,
  computation_version VARCHAR(16) DEFAULT 'v3',
  computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_cd_brand (cd_market_id, cd_brand_id, source, measure)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""
STRATEGIC_CD_MARKET_DDL = STRATEGIC_ML_MARKET_DDL.replace("mart_strategic_ml_market_metric", "mart_strategic_cd_market_metric").replace("ml_id VARCHAR(32) NOT NULL,\n  ml_name", "cd_market_id VARCHAR(32) NOT NULL,\n  cd_market_name").replace("uq_ml_market (ml_id", "uq_cd_market (cd_market_id")


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


_validate_schema_pair = validate_mart_schema_pair


def _ensure_isolated_schema(target_db: str, source_db: str) -> None:
    _validate_schema_pair(target_db, source_db)
    conn = _admin_connect(_env())
    try:
        with conn.cursor() as cur:
            cur.execute(f"CREATE DATABASE IF NOT EXISTS `{target_db}` DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci")
            cur.execute(f"USE `{target_db}`")
            for table in ("mart_strategic_ml_brand_metric", "mart_strategic_ml_market_metric", "mart_strategic_cd_brand_metric", "mart_strategic_cd_market_metric"):
                cur.execute(f"DROP TABLE IF EXISTS {table}")
            for ddl in (STRATEGIC_ML_BRAND_DDL, STRATEGIC_ML_MARKET_DDL, STRATEGIC_CD_BRAND_DDL, STRATEGIC_CD_MARKET_DDL):
                cur.execute(ddl)
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
        if params.get("catalog_root"):
            os.environ["S5_CATALOG_DIR"] = str(params["catalog_root"])
        _ensure_isolated_schema(target_db, source_db)
        _configure_mart_env(target_db, source_db)
        from pipeline.etl.io.mart.strategic_cd import compute_strategic_cd
        from pipeline.etl.io.mart.strategic_ml import compute_strategic_ml

        market_id = str(params.get("ml_id") or "").strip()
        run_ml = not market_id or market_id.startswith("ml_")
        run_cd = not market_id or market_id.startswith("cd_")
        if run_ml:
            _, _, ml_stats = compute_strategic_ml(False, True, Path("/tmp"), ml=market_id or None)
            print(f"[{STAGE}] ml: brand_rows={ml_stats['brand_rows']} market_rows={ml_stats['market_rows']} ml_count={ml_stats['ml_count']}")
        if run_cd:
            _, _, cd_stats = compute_strategic_cd(False, True, Path("/tmp"), cd_market=market_id or None)
            print(f"[{STAGE}] cd: brand_rows={cd_stats['brand_rows']} market_rows={cd_stats['market_rows']} cd_market_count={cd_stats['cd_market_count']}")
    except Exception as exc:
        print(f"[{STAGE}] 실패: {exc}")
        return 1
    print(f"[{STAGE}] 완료 target_db={target_db} source_db={source_db}")
    return 0
