-- ===========================================================
-- Migration 012 - response_store cache metadata for six marts
-- ===========================================================
-- Phase: 16-G-4-Fix-Cache
-- Dependencies: 011_jsonb_six_mart.sql
-- Note: _migration_state is written by pipeline/scripts/run_migration.py.
--
-- This migration keeps the existing response_store primary-key cache contract
-- used by the FastAPI code, while adding lookup metadata for the six-mart
-- precomputed response cache.
-- ===========================================================

USE jw_mart;

ALTER TABLE response_store
  MODIFY cache_key VARCHAR(512) NOT NULL,
  ADD COLUMN brand_key VARCHAR(255) NULL AFTER brand_name,
  ADD COLUMN market_id VARCHAR(64) NULL AFTER brand_key,
  ADD COLUMN view_type VARCHAR(32) NULL AFTER view,
  ADD COLUMN payload JSON NULL AFTER response_json,
  ADD INDEX idx_endpoint_view_type (endpoint, view_type),
  ADD INDEX idx_brand_cache (endpoint, brand_key, source, measure),
  ADD INDEX idx_market_cache (endpoint, market_id, source, measure);
