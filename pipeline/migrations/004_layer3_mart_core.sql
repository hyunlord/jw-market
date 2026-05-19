-- ===========================================================
-- Migration 004 - Layer 3 mart_core
-- ===========================================================
-- Pre-computed brand metric per:
--   market (ml_id) x brand x period_yyyymm x channel x specialty
--
-- Phase: 16-E-1
-- Dependencies: 001_layer1_raw_tables.sql, 003_layer1_hybrid_split.sql
-- Note: _migration_state is written by pipeline/scripts/run_migration.py.
-- ===========================================================

USE jw_mart;

CREATE TABLE IF NOT EXISTS mart_core_brand_metric (
  id              BIGINT AUTO_INCREMENT PRIMARY KEY,

  -- Dimension
  ml_id           VARCHAR(16) NOT NULL COMMENT 'market id (ml_001~ml_016)',
  brand_id        VARCHAR(32) NOT NULL COMMENT 'strategic_brand.brand_id',
  brand_name      VARCHAR(255) NOT NULL,
  is_jw           BOOLEAN NOT NULL DEFAULT FALSE COMMENT 'JW 25 brand flag',
  period_yyyymm   CHAR(7) NOT NULL COMMENT 'YYYY-MM',
  channel         VARCHAR(32) NULL COMMENT 'TH/GH/Semi/CL/PHC/Other (NULL = all)',
  specialty       VARCHAR(32) NULL COMMENT 'IGF/Cardio/Endo/Nephro/Neuro/Uro/GI (NULL = all)',

  -- NULL-safe helpers for UNIQUE KEY.
  channel_norm    VARCHAR(32) AS (COALESCE(channel, '__ALL__')) STORED INVISIBLE,
  specialty_norm  VARCHAR(32) AS (COALESCE(specialty, '__ALL__')) STORED INVISIBLE,

  -- 7 Metrics
  market_share    DECIMAL(8,5)  NULL COMMENT 'MS 0.00000~1.00000',
  mom             DECIMAL(10,4) NULL COMMENT 'Month-over-Month growth rate',
  qoq             DECIMAL(10,4) NULL COMMENT 'Quarter-over-Quarter growth rate',
  yoy             DECIMAL(10,4) NULL COMMENT 'Year-over-Year growth rate',
  mat             DECIMAL(20,2) NULL COMMENT 'Moving Annual Total (previous 12-month sum)',
  growth_abs      DECIMAL(20,2) NULL COMMENT 'Absolute growth (this period - previous period)',
  rank_in_market  INT           NULL COMMENT 'Rank in ml x period x channel x specialty',

  -- Raw values used for metric calculation
  raw_value       DECIMAL(20,2) NULL COMMENT 'Sum of Layer 2 canonical_value',
  raw_count       INT           NULL COMMENT 'Layer 2 row count',

  -- Metadata
  payload         JSON          NULL COMMENT 'Source split and debug metadata',
  computed_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  computation_version VARCHAR(16) NOT NULL DEFAULT 'v1' COMMENT 'Metric logic version',

  UNIQUE KEY uq_metric (ml_id, brand_id, period_yyyymm, channel_norm, specialty_norm),
  INDEX idx_ml_period (ml_id, period_yyyymm),
  INDEX idx_brand_period (brand_id, period_yyyymm),
  INDEX idx_jw_period (is_jw, period_yyyymm),
  INDEX idx_ml_period_rank (ml_id, period_yyyymm, rank_in_market)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Layer 3 mart_core - pre-computed brand metric per market x period x channel x specialty';
