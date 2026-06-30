-- Phase zeta short/long run-ledger support.
--
-- Apply target:
--   Agent2 generation databases where zeta_analysis_runs exists.
--
-- Do NOT apply this migration to the operational cache DB when the only goal is
-- exposing ai_analysis_short_json/ai_analysis_long_json. Production jw_mart may
-- not contain zeta_analysis_runs; applying this file there can fail before the
-- cache columns are added.
--
-- Required preflight before applying:
--   SELECT brand, snapshot_at, COUNT(*) c
--   FROM zeta_analysis_runs
--   GROUP BY brand, snapshot_at
--   HAVING c > 1;
--
-- If this returns rows, resolve/triage duplicates before replacing the unique
-- key. Existing legacy rows receive analysis_variant='legacy'.

ALTER TABLE zeta_analysis_runs
  ADD COLUMN IF NOT EXISTS analysis_variant VARCHAR(16) NOT NULL DEFAULT 'legacy' AFTER snapshot_at;

ALTER TABLE zeta_analysis_runs
  DROP INDEX IF EXISTS uq_brand_snapshot,
  ADD UNIQUE KEY IF NOT EXISTS uq_brand_snapshot_variant (brand, snapshot_at, analysis_variant);

-- Rollback (PL approval required; non-additive for short/long run history):
-- ALTER TABLE zeta_analysis_runs
--   DROP INDEX IF EXISTS uq_brand_snapshot_variant,
--   ADD UNIQUE KEY IF NOT EXISTS uq_brand_snapshot (brand, snapshot_at);
-- ALTER TABLE zeta_analysis_runs
--   DROP COLUMN analysis_variant;
