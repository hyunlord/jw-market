-- ===========================================================
-- Migration 009 - DECIMAL extension for ratio metrics
-- ===========================================================
-- Phase: 16-G-3-Fix
-- 16-G-3-A audit found growth_contribution can exceed DECIMAL(10,4)
-- when market growth is close to zero.
--
-- Dependencies: 008_cd_market_mart.sql
-- Note: _migration_state is written by pipeline/scripts/run_migration.py.
-- ===========================================================

USE jw_mart;

ALTER TABLE mart_core_brand_metric
  MODIFY COLUMN growth_contribution DECIMAL(20,4) NULL
    COMMENT 'Growth Contribution (brand growth / market growth x 100, denominator threshold 10000)',
  MODIFY COLUMN ei_5y DECIMAL(20,4) NULL
    COMMENT 'Evolution Index (brand cagr_5y / market cagr_5y x 100, denominator threshold 0.001)';

ALTER TABLE mart_cd_market_metric
  MODIFY COLUMN growth_contribution DECIMAL(20,4) NULL
    COMMENT 'Growth Contribution (brand growth / market growth x 100, denominator threshold 10000)',
  MODIFY COLUMN ei_5y DECIMAL(20,4) NULL
    COMMENT 'Evolution Index (brand cagr_5y / market cagr_5y x 100, denominator threshold 0.001)';
