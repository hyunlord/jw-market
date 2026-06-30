-- Phase ζ short/long variant support.
-- Existing legacy rows remain analysis_variant='legacy'.

ALTER TABLE zeta_analysis_runs
  ADD COLUMN analysis_variant VARCHAR(16) NOT NULL DEFAULT 'legacy' AFTER snapshot_at;

ALTER TABLE zeta_analysis_runs
  DROP INDEX uq_brand_snapshot,
  ADD UNIQUE KEY uq_brand_snapshot_variant (brand, snapshot_at, analysis_variant);

ALTER TABLE cache_deep_analysis_ai_analysis
  ADD COLUMN ai_analysis_short_json LONGTEXT AFTER ai_analysis_json,
  ADD COLUMN ai_analysis_long_json LONGTEXT AFTER ai_analysis_short_json;
