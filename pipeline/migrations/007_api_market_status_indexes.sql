-- ===========================================================
-- Migration 007 - API market-status indexes
-- ===========================================================
-- Phase: 16-H
-- Dependencies: 006_response_store.sql
-- Note: _migration_state is written by pipeline/scripts/run_migration.py.
-- ===========================================================

USE jw_mart;

ALTER TABLE mart_core_brand_metric
  ADD INDEX idx_market_status_latest (
    channel_norm,
    specialty_norm,
    ml_id,
    period_yyyymm,
    rank_in_market
  ),
  ADD INDEX idx_market_status_period (
    channel_norm,
    specialty_norm,
    period_yyyymm,
    ml_id,
    rank_in_market
  );
