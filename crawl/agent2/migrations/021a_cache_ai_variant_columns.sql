-- Phase zeta short/long cache exposure.
--
-- Operational target:
--   jw_mart.cache_deep_analysis_ai_analysis
--
-- This migration is intentionally additive. It adds nullable sibling payload
-- columns for short/long Agent2 narratives and does not touch ai_analysis_json.

ALTER TABLE cache_deep_analysis_ai_analysis
  ADD COLUMN IF NOT EXISTS ai_analysis_short_json LONGTEXT AFTER ai_analysis_json,
  ADD COLUMN IF NOT EXISTS ai_analysis_long_json LONGTEXT AFTER ai_analysis_short_json;

-- Rollback (PL approval required; destructive for generated short/long payloads):
-- ALTER TABLE cache_deep_analysis_ai_analysis
--   DROP COLUMN ai_analysis_short_json,
--   DROP COLUMN ai_analysis_long_json;
