-- ===========================================================
-- Migration 011 - JSON Layer 3 six mart schema
-- ===========================================================
-- Phase: 16-G-4-Fix-Schema-v2
-- Design-v2 replaces the row-per-period marts with row-per-brand
-- and row-per-market JSON marts. Layer 1/2 staging and catalog files
-- remain unchanged; ETL v3 will populate these tables in a later phase.
--
-- Dependencies: 010_source_dimension.sql
-- Note: _migration_state is written by pipeline/scripts/run_migration.py.
-- ===========================================================

USE jw_mart;

DROP TABLE IF EXISTS mart_core_brand_metric;
DROP TABLE IF EXISTS mart_cd_market_metric;

CREATE TABLE mart_general_brand_metric (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,

  brand_key       VARCHAR(255) NOT NULL COMMENT 'ETL v3 normalized brand identifier',
  brand_name      VARCHAR(255) NOT NULL COMMENT 'Display brand name',
  atc4_code       VARCHAR(16) NOT NULL COMMENT 'ATC4 market code extracted from raw data',
  atc4_desc       VARCHAR(255) NULL COMMENT 'ATC4 description when available',
  source          VARCHAR(16) NOT NULL COMMENT 'ubist or iqvia_nsa',
  measure         VARCHAR(32) NOT NULL COMMENT 'sales, volume, unit, dosage_unit, counting_unit',
  unit_label      VARCHAR(32) NOT NULL COMMENT 'KRW, Rx, unit, dosage unit, counting unit',

  metric_history          JSON NOT NULL COMMENT 'Period keyed 7 base metrics',
  extended_metric_history JSON NOT NULL COMMENT 'Period keyed 8 extended metrics',
  channel_data            JSON NOT NULL COMMENT 'Channel keyed period metrics',
  specialty_data          JSON NOT NULL COMMENT 'Specialty keyed period metrics',
  dimension_data          JSON NOT NULL COMMENT 'SKU dimension keyed period metrics',
  dimension_channel_data  JSON NOT NULL COMMENT 'SKU dimension x channel keyed period metrics',
  by_dimension            JSON NOT NULL COMMENT 'Class/molecule/dosage/nhi/ox-gx/audit dimensions',
  raw_value_history       JSON NOT NULL COMMENT 'Period keyed raw values',

  payload         JSON NULL,
  computation_version VARCHAR(16) DEFAULT 'v3',
  computed_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

  UNIQUE KEY uq_general_brand (brand_key, atc4_code, source, measure),
  INDEX idx_general_brand_key (brand_key, source, measure),
  INDEX idx_general_brand_atc4 (atc4_code, source, measure),
  INDEX idx_general_brand_source (source, measure)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='General market landscape brand mart: brand x ATC4 x source x measure';

CREATE TABLE mart_general_market_metric (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,

  atc4_code       VARCHAR(16) NOT NULL COMMENT 'ATC4 market code',
  atc4_desc       VARCHAR(255) NULL COMMENT 'ATC4 description when available',
  source          VARCHAR(16) NOT NULL COMMENT 'ubist or iqvia_nsa',
  measure         VARCHAR(32) NOT NULL COMMENT 'sales, volume, unit, dosage_unit, counting_unit',
  unit_label      VARCHAR(32) NOT NULL COMMENT 'KRW, Rx, unit, dosage unit, counting unit',

  market_size_series              JSON NOT NULL,
  hhi_series                      JSON NOT NULL,
  brand_ranking                   JSON NOT NULL,
  company_ranking_stacked         JSON NOT NULL,
  company_concentration_trend     JSON NOT NULL,
  ei_ms_matrix                    JSON NOT NULL,
  growth_contribution_ms_matrix   JSON NOT NULL,
  growth_contribution             JSON NOT NULL,
  analysis_levels                 JSON NOT NULL,
  level_top5_trend                JSON NOT NULL,
  target_customer_competition     JSON NOT NULL,

  payload         JSON NULL,
  computation_version VARCHAR(16) DEFAULT 'v3',
  computed_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

  UNIQUE KEY uq_general_market (atc4_code, source, measure),
  INDEX idx_general_market_atc4 (atc4_code),
  INDEX idx_general_market_source (source, measure)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='General market landscape market mart: ATC4 x source x measure';

CREATE TABLE mart_strategic_ml_brand_metric (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,

  ml_id           VARCHAR(16) NOT NULL COMMENT 'Strategic ml_market identifier',
  brand_id        VARCHAR(32) NOT NULL COMMENT 'strategic_brand.brand_id',
  brand_key       VARCHAR(255) NOT NULL COMMENT 'ETL v3 normalized brand identifier',
  brand_name      VARCHAR(255) NOT NULL,
  source          VARCHAR(16) NOT NULL COMMENT 'ubist or iqvia_nsa',
  measure         VARCHAR(32) NOT NULL COMMENT 'sales, volume, unit, dosage_unit, counting_unit',
  is_jw           BOOLEAN NOT NULL DEFAULT FALSE,
  unit_label      VARCHAR(32) NOT NULL,

  metric_history          JSON NOT NULL,
  extended_metric_history JSON NOT NULL,
  channel_data            JSON NOT NULL,
  specialty_data          JSON NOT NULL,
  dimension_data          JSON NOT NULL,
  dimension_channel_data  JSON NOT NULL,
  by_dimension            JSON NOT NULL,
  raw_value_history       JSON NOT NULL,
  overlay_data            JSON NULL COMMENT 'Strategic catalog overlay and overridden fields',

  payload         JSON NULL,
  computation_version VARCHAR(16) DEFAULT 'v3',
  computed_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

  UNIQUE KEY uq_ml_brand (ml_id, brand_id, source, measure),
  INDEX idx_ml_brand_brand (brand_id, source, measure),
  INDEX idx_ml_brand_ml (ml_id, source, measure),
  INDEX idx_ml_brand_jw (is_jw, source, measure)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Strategic ML brand mart: ml_market x brand x source x measure';

CREATE TABLE mart_strategic_ml_market_metric (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,

  ml_id           VARCHAR(16) NOT NULL COMMENT 'Strategic ml_market identifier',
  ml_name         VARCHAR(255) NULL,
  source          VARCHAR(16) NOT NULL COMMENT 'ubist or iqvia_nsa',
  measure         VARCHAR(32) NOT NULL COMMENT 'sales, volume, unit, dosage_unit, counting_unit',
  unit_label      VARCHAR(32) NOT NULL,

  market_size_series              JSON NOT NULL,
  hhi_series_5y                   JSON NOT NULL,
  brand_ranking_stacked           JSON NOT NULL,
  company_ranking_stacked         JSON NOT NULL,
  company_concentration_trend     JSON NOT NULL,
  ei_ms_matrix                    JSON NOT NULL,
  growth_contribution_ms_matrix   JSON NOT NULL,
  growth_contribution             JSON NOT NULL,
  analysis_levels                 JSON NOT NULL,
  level_top5_trend                JSON NOT NULL,
  target_customer_competition     JSON NOT NULL,

  payload         JSON NULL,
  computation_version VARCHAR(16) DEFAULT 'v3',
  computed_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

  UNIQUE KEY uq_ml_market (ml_id, source, measure),
  INDEX idx_ml_market_source (source, measure)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Strategic ML market mart: ml_market x source x measure';

CREATE TABLE mart_strategic_cd_brand_metric (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,

  cd_market_id    VARCHAR(16) NOT NULL COMMENT 'Competitive dynamics market identifier',
  cd_brand_id     VARCHAR(32) NOT NULL COMMENT 'cd_brand.brand_id',
  brand_key       VARCHAR(255) NOT NULL COMMENT 'ETL v3 normalized brand identifier',
  brand_name      VARCHAR(255) NOT NULL,
  source          VARCHAR(16) NOT NULL COMMENT 'ubist or iqvia_nsa',
  measure         VARCHAR(32) NOT NULL COMMENT 'sales, volume, unit, dosage_unit, counting_unit',
  is_jw           BOOLEAN NOT NULL DEFAULT FALSE,
  unit_label      VARCHAR(32) NOT NULL,

  metric_history          JSON NOT NULL,
  extended_metric_history JSON NOT NULL,
  channel_data            JSON NOT NULL,
  specialty_data          JSON NOT NULL,
  dimension_data          JSON NOT NULL,
  dimension_channel_data  JSON NOT NULL,
  by_dimension            JSON NOT NULL,
  raw_value_history       JSON NOT NULL,
  cd_overlay              JSON NULL COMMENT 'cd catalog class additions and overrides',
  overlay_data            JSON NULL COMMENT 'Strategic catalog overlay and overridden fields',

  payload         JSON NULL,
  computation_version VARCHAR(16) DEFAULT 'v3',
  computed_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

  UNIQUE KEY uq_cd_brand (cd_market_id, cd_brand_id, source, measure),
  INDEX idx_cd_brand_brand (cd_brand_id, source, measure),
  INDEX idx_cd_brand_market (cd_market_id, source, measure),
  INDEX idx_cd_brand_jw (is_jw, source, measure)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Strategic CD brand mart: cd_market x brand x source x measure';

CREATE TABLE mart_strategic_cd_market_metric (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,

  cd_market_id    VARCHAR(16) NOT NULL COMMENT 'Competitive dynamics market identifier',
  cd_market_name  VARCHAR(255) NULL,
  source          VARCHAR(16) NOT NULL COMMENT 'ubist or iqvia_nsa',
  measure         VARCHAR(32) NOT NULL COMMENT 'sales, volume, unit, dosage_unit, counting_unit',
  unit_label      VARCHAR(32) NOT NULL,

  market_size_series              JSON NOT NULL,
  hhi_series_5y                   JSON NOT NULL,
  brand_ranking_stacked           JSON NOT NULL,
  company_ranking_stacked         JSON NOT NULL,
  company_concentration_trend     JSON NOT NULL,
  ei_ms_matrix                    JSON NOT NULL,
  growth_contribution_ms_matrix   JSON NOT NULL,
  growth_contribution             JSON NOT NULL,
  analysis_levels                 JSON NOT NULL,
  level_top5_trend                JSON NOT NULL,
  target_customer_competition     JSON NOT NULL,

  payload         JSON NULL,
  computation_version VARCHAR(16) DEFAULT 'v3',
  computed_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

  UNIQUE KEY uq_cd_market (cd_market_id, source, measure),
  INDEX idx_cd_market_source (source, measure)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Strategic CD market mart: cd_market x source x measure';
