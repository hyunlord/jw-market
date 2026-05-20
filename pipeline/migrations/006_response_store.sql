-- ===========================================================
-- Migration 006 - Layer 4 response_store
-- ===========================================================
-- Phase: 16-F-2
-- Dependencies: 005_layer3_extended_metric.sql
-- Note: _migration_state is written by pipeline/scripts/run_migration.py.
-- ===========================================================

USE jw_mart;

CREATE TABLE IF NOT EXISTS response_store (
  cache_key       VARCHAR(255) NOT NULL PRIMARY KEY,
  endpoint        VARCHAR(64) NOT NULL,
  brand_name      VARCHAR(255) NULL,
  period_yyyymm   CHAR(7) NULL,
  view            VARCHAR(32) NULL,
  source          VARCHAR(16) NULL,
  measure         VARCHAR(32) NULL,

  response_json   JSON NOT NULL,
  ttl_seconds     INT DEFAULT 86400,
  computed_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  expires_at      TIMESTAMP NULL,

  computation_ms  INT NULL,
  size_bytes      INT NULL,

  INDEX idx_endpoint (endpoint),
  INDEX idx_brand_period (brand_name, period_yyyymm),
  INDEX idx_expires (expires_at),
  INDEX idx_endpoint_brand (endpoint, brand_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Layer 4 response_store - pre-computed API response cache';
