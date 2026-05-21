-- ===========================================================
-- Migration 013 - endpoint-specific Layer 4 cache tables
-- ===========================================================
-- Phase: 16-G-4-Fix-CacheSplit
-- Dependencies: 012_response_store_cache_v2.sql
--
-- Split the generic response_store table into four endpoint-specific cache
-- tables. The legacy table is retained for reconciliation and later cleanup.
-- ===========================================================

USE jw_mart;

RENAME TABLE response_store TO response_store_legacy;

CREATE TABLE cache_brands (
  view_type    VARCHAR(32) NOT NULL,
  source       VARCHAR(16) NOT NULL,
  response_json LONGTEXT NOT NULL,
  payload_size INT UNSIGNED NOT NULL DEFAULT 0,
  updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

  PRIMARY KEY (view_type, source)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Layer 4 cache for GET /api/brands';

CREATE TABLE cache_market_status (
  view_type    VARCHAR(32) NOT NULL,
  market_id    VARCHAR(64) NOT NULL,
  source       VARCHAR(16) NOT NULL,
  measure      VARCHAR(32) NOT NULL,
  market_name  VARCHAR(255) NULL,
  response_json LONGTEXT NOT NULL,
  payload_size INT UNSIGNED NOT NULL DEFAULT 0,
  updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

  PRIMARY KEY (view_type, market_id, source, measure)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Layer 4 cache for GET /api/market-status';

CREATE TABLE cache_cause (
  view_type    VARCHAR(32) NOT NULL,
  brand_key    VARCHAR(255) NOT NULL,
  market_id    VARCHAR(64) NOT NULL,
  source       VARCHAR(16) NOT NULL,
  measure      VARCHAR(32) NOT NULL,
  brand_name   VARCHAR(255) NULL,
  is_jw        BOOLEAN NULL,
  response_json LONGTEXT NOT NULL,
  payload_size INT UNSIGNED NOT NULL DEFAULT 0,
  updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

  PRIMARY KEY (view_type, brand_key, market_id, source, measure),
  INDEX idx_cache_cause_market_join (view_type, market_id, source, measure),
  INDEX idx_cache_cause_brand_name (brand_name),
  INDEX idx_cache_cause_brand_key (brand_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Layer 4 cache for GET /api/cause';

CREATE TABLE cache_deep_analysis (
  view_type    VARCHAR(32) NOT NULL,
  brand_key    VARCHAR(255) NOT NULL,
  market_id    VARCHAR(64) NOT NULL,
  source       VARCHAR(16) NOT NULL,
  measure      VARCHAR(32) NOT NULL,
  brand_name   VARCHAR(255) NULL,
  is_jw        BOOLEAN NULL,
  response_json LONGTEXT NOT NULL,
  payload_size INT UNSIGNED NOT NULL DEFAULT 0,
  updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

  PRIMARY KEY (view_type, brand_key, market_id, source, measure),
  INDEX idx_cache_deep_analysis_brand_name (brand_name),
  INDEX idx_cache_deep_analysis_brand_key (brand_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Layer 4 cache for GET /api/deep-analysis';
