-- ===========================================================
-- Migration 008 - cd_market mart
-- ===========================================================
-- Phase: 16-G-1
-- Competitive Dynamics mart for 19 cd_market definitions.
--
-- Dependencies: 007_api_market_status_indexes.sql
-- Note: _migration_state is written by pipeline/scripts/run_migration.py.
-- ===========================================================

USE jw_mart;

CREATE TABLE IF NOT EXISTS mart_cd_market_metric (
  id              BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,

  -- cd_market dimension
  cd_market_id    VARCHAR(16) NOT NULL COMMENT 'cd_market.cd_id (cd_001~cd_019)',
  cd_brand_id     VARCHAR(32) NOT NULL COMMENT 'cd_brand.brand_id',
  cd_brand_name   VARCHAR(255) NOT NULL,
  ml_id           VARCHAR(16) NOT NULL COMMENT 'parent ml_market id',
  is_jw           BOOLEAN NOT NULL DEFAULT FALSE COMMENT 'JW display brand flag',

  -- Period and aggregation dimensions
  period_yyyymm   CHAR(7) NOT NULL COMMENT 'YYYY-MM',
  channel         VARCHAR(32) NULL COMMENT 'NULL = all channels',
  specialty       VARCHAR(32) NULL COMMENT 'NULL = all specialties',
  channel_norm    VARCHAR(32) AS (COALESCE(channel, '__ALL__')) STORED INVISIBLE,
  specialty_norm  VARCHAR(32) AS (COALESCE(specialty, '__ALL__')) STORED INVISIBLE,

  -- Raw aggregation
  raw_value       DECIMAL(20,4) NULL COMMENT 'Sum of Layer 2 canonical_value within cd_market',
  raw_count       INT           NULL COMMENT 'Layer 2 row count',

  -- 7 base metrics
  market_share    DECIMAL(10,6) NULL COMMENT 'Share within cd_market slice',
  mom             DECIMAL(10,4) NULL COMMENT 'Month-over-Month growth rate',
  qoq             DECIMAL(10,4) NULL COMMENT 'Quarter-over-Quarter growth rate',
  yoy             DECIMAL(10,4) NULL COMMENT 'Year-over-Year growth rate',
  mat             DECIMAL(20,4) NULL COMMENT 'Moving Annual Total (previous 12-month sum)',
  growth_abs      DECIMAL(20,4) NULL COMMENT 'Absolute growth (this period - previous period)',
  rank_in_market  INT           NULL COMMENT 'Rank in cd_market x period x channel x specialty',

  -- 8 extended metrics
  cagr_1y             DECIMAL(10,4) NULL COMMENT 'CAGR 1Y',
  cagr_3y             DECIMAL(10,4) NULL COMMENT 'CAGR 3Y',
  cagr_5y             DECIMAL(10,4) NULL COMMENT 'CAGR 5Y',
  ei_5y               DECIMAL(10,4) NULL COMMENT 'Evolution Index (brand cagr_5y / market cagr_5y x 100)',
  momentum_score      DECIMAL(10,4) NULL COMMENT 'Recent 4-quarter market-share slope',
  growth_contribution DECIMAL(10,4) NULL COMMENT 'Brand growth / market growth x 100',
  hhi                 DECIMAL(10,2) NULL COMMENT 'Herfindahl-Hirschman Index for cd_market slice',
  market_cagr_5y      DECIMAL(10,4) NULL COMMENT 'cd_market total 5Y CAGR',

  -- Metadata
  payload         JSON NULL COMMENT 'Source split, filters, and debug metadata',
  computation_version VARCHAR(16) NOT NULL DEFAULT 'v1' COMMENT 'Base metric logic version',
  extended_metric_version VARCHAR(16) NOT NULL DEFAULT 'v1' COMMENT 'Extended metric logic version',
  computed_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

  UNIQUE KEY uq_cd_metric (
    cd_market_id,
    cd_brand_id,
    period_yyyymm,
    channel_norm,
    specialty_norm
  ),
  INDEX idx_cd_market_period (cd_market_id, period_yyyymm),
  INDEX idx_cd_brand_period (cd_brand_id, period_yyyymm),
  INDEX idx_cd_jw_period (is_jw, period_yyyymm),
  INDEX idx_cd_market_period_rank (cd_market_id, period_yyyymm, rank_in_market),
  INDEX idx_cd_total_level_latest (
    channel_norm,
    specialty_norm,
    cd_market_id,
    period_yyyymm,
    rank_in_market
  ),
  INDEX idx_cd_total_level_period (
    channel_norm,
    specialty_norm,
    period_yyyymm,
    cd_market_id,
    rank_in_market
  )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Layer 3 mart for cd_market competitive dynamics';
