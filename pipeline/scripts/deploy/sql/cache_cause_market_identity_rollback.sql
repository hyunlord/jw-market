-- Rollback for cache_cause_market_identity.sql
--
-- STATUS: DESIGNED, NOT APPLIED.
--
-- Step 2 first (restore the original primary key), then step 1.
-- Dropping the columns is safe for readers: nothing reads view_source_id,
-- run_id, build_sha or input_manifest_json from cache_cause today. The reader
-- fix matches on market_id, which this rollback does not touch.

-- undo step 2
-- ALTER TABLE cache_cause
--   DROP PRIMARY KEY,
--   ADD PRIMARY KEY (brand, view_type, source, measure, market_id),
--   MODIFY COLUMN view_source_id VARCHAR(32) NULL;

-- undo step 1
DROP INDEX IF EXISTS idx_cache_cause_run ON cache_cause;
DROP INDEX IF EXISTS idx_cache_cause_view_source ON cache_cause;

ALTER TABLE cache_cause
  DROP COLUMN IF EXISTS input_manifest_json,
  DROP COLUMN IF EXISTS build_sha,
  DROP COLUMN IF EXISTS run_id,
  DROP COLUMN IF EXISTS view_source_id;
