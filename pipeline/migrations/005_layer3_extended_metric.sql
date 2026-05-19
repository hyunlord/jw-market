-- ===========================================================
-- Migration 005 - Layer 3 mart_core extended metrics
-- ===========================================================
-- Phase: 16-E-4-A
-- Source:
--   시장분석 에이전트 카테고리 (260506).xlsx
--   sheets: 원인 분석, 원인분석_상세
--
-- Dependencies: 004_layer3_mart_core.sql
-- Note: _migration_state is written by pipeline/scripts/run_migration.py.
-- ===========================================================

USE jw_mart;

ALTER TABLE mart_core_brand_metric
  -- Brand-level metrics
  ADD COLUMN cagr_1y DECIMAL(10,4) NULL COMMENT 'CAGR 1Y',
  ADD COLUMN cagr_3y DECIMAL(10,4) NULL COMMENT 'CAGR 3Y',
  ADD COLUMN cagr_5y DECIMAL(10,4) NULL COMMENT 'CAGR 5Y',
  ADD COLUMN ei_5y DECIMAL(10,4) NULL COMMENT 'Evolution Index (brand cagr_5y / market cagr_5y x 100)',
  ADD COLUMN momentum_score DECIMAL(10,4) NULL COMMENT 'Momentum score (recent 4-quarter MS slope)',
  ADD COLUMN growth_contribution DECIMAL(10,4) NULL COMMENT 'Growth contribution (brand growth / market growth x 100)',

  -- Market-level metrics, repeated on each brand row for the same market slice.
  ADD COLUMN hhi DECIMAL(10,2) NULL COMMENT 'Herfindahl-Hirschman Index',
  ADD COLUMN market_cagr_5y DECIMAL(10,4) NULL COMMENT 'Market total 5Y CAGR',

  -- Extended metric metadata
  ADD COLUMN extended_metric_version VARCHAR(16) DEFAULT 'v1' COMMENT 'Extended metric logic version';

ALTER TABLE mart_core_brand_metric
  ADD INDEX idx_brand_cagr5y (brand_id, period_yyyymm, cagr_5y),
  ADD INDEX idx_brand_momentum (brand_id, period_yyyymm, momentum_score);
