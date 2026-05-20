-- ===========================================================
-- Migration 010 - source/measure dimension for Layer 3 marts
-- ===========================================================
-- Phase: 16-G-4-Fix-Schema
-- DeepAudit-2 confirmed Layer 3 marts aggregate source-agnostic
-- canonical values. PL decision: keep Layer 2 staging intact, exclude
-- CHSO/CSD from future Layer 3 reloads, and rebuild Layer 3 from
-- source-separated UBIST + IQVIA NSA only.
--
-- Dependencies: 009_decimal_extend.sql
-- Note: _migration_state is written by pipeline/scripts/run_migration.py.
-- ===========================================================

USE jw_mart;

-- Existing canonical caches are invalid once source becomes part of the
-- Layer 3 row identity. ETL reload happens in the next phase.
TRUNCATE TABLE response_store;
TRUNCATE TABLE mart_core_brand_metric;
TRUNCATE TABLE mart_cd_market_metric;

ALTER TABLE mart_core_brand_metric
  ADD COLUMN source VARCHAR(16) NOT NULL COMMENT 'Source dimension: ubist or iqvia_nsa' AFTER ml_id,
  ADD COLUMN measure VARCHAR(16) NOT NULL COMMENT 'Measure dimension within source' AFTER source;

ALTER TABLE mart_core_brand_metric
  DROP INDEX uq_metric;

ALTER TABLE mart_core_brand_metric
  ADD UNIQUE KEY uq_metric_with_source (
    ml_id,
    brand_id,
    source,
    measure,
    period_yyyymm,
    channel_norm,
    specialty_norm
  ),
  ADD INDEX idx_source_period (source, measure, period_yyyymm),
  ADD INDEX idx_brand_source_period (brand_id, source, measure, period_yyyymm);

ALTER TABLE mart_cd_market_metric
  ADD COLUMN source VARCHAR(16) NOT NULL COMMENT 'Source dimension: ubist or iqvia_nsa' AFTER cd_market_id,
  ADD COLUMN measure VARCHAR(16) NOT NULL COMMENT 'Measure dimension within source' AFTER source;

ALTER TABLE mart_cd_market_metric
  DROP INDEX uq_cd_metric;

ALTER TABLE mart_cd_market_metric
  ADD UNIQUE KEY uq_cd_metric_with_source (
    cd_market_id,
    cd_brand_id,
    source,
    measure,
    period_yyyymm,
    channel_norm,
    specialty_norm
  ),
  ADD INDEX idx_cd_source_period (source, measure, period_yyyymm),
  ADD INDEX idx_cd_brand_source_period (cd_brand_id, source, measure, period_yyyymm);
