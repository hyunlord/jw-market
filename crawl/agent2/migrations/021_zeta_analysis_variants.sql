-- Phase ζ short/long variant support.
-- Existing legacy rows remain analysis_variant='legacy'.

ALTER TABLE zeta_analysis_runs
  ADD COLUMN IF NOT EXISTS analysis_variant VARCHAR(16) NOT NULL DEFAULT 'legacy' AFTER snapshot_at;

ALTER TABLE zeta_analysis_runs
  DROP INDEX IF EXISTS uq_brand_snapshot,
  ADD UNIQUE KEY IF NOT EXISTS uq_brand_snapshot_variant (brand, snapshot_at, analysis_variant);

ALTER TABLE cache_deep_analysis_ai_analysis
  ADD COLUMN IF NOT EXISTS ai_analysis_short_json LONGTEXT AFTER ai_analysis_json,
  ADD COLUMN IF NOT EXISTS ai_analysis_long_json LONGTEXT AFTER ai_analysis_short_json;

-- Rollback (PL approval required; destructive for generated short/long payloads):
-- ALTER TABLE cache_deep_analysis_ai_analysis
--   DROP COLUMN ai_analysis_short_json,
--   DROP COLUMN ai_analysis_long_json;
