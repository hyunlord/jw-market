-- cache_cause market identity + provenance
--
-- STATUS: DESIGNED, NOT APPLIED. Applying this is a separate PL gate.
--
-- Why
--   1. market_id is derived from the parent ML for BOTH views, so two sibling CD
--      markets under one ML share a primary key and REPLACE INTO silently drops
--      one. Known splits: ml_008 -> cd_008/cd_009, ml_009 -> cd_010/cd_011,
--      ml_010 -> cd_012/cd_013.
--   2. cache_cause has updated_at but no run identity, so no row can be traced
--      to the S6 execution that produced it. cache_brands and
--      cache_market_status already carry build_sha/input_manifest_json.
--
-- Step 1 is additive and reversible on its own; the producer fills these columns
-- only when they exist, so step 1 can ship and bake before step 2.
-- Step 2 changes the primary key and MUST NOT run until step 1 has backfilled
-- every row (guard below returns 0).

-- ---------------------------------------------------------------- step 1 ----
ALTER TABLE cache_cause
  ADD COLUMN IF NOT EXISTS view_source_id VARCHAR(32) NULL
      COMMENT 'actual market: ml_id for market_landscape, cd_id for competitive_dynamics',
  ADD COLUMN IF NOT EXISTS run_id VARCHAR(64) NULL
      COMMENT 'S6 execution that produced the row',
  ADD COLUMN IF NOT EXISTS build_sha VARCHAR(64) NULL,
  ADD COLUMN IF NOT EXISTS input_manifest_json LONGTEXT NULL;

CREATE INDEX IF NOT EXISTS idx_cache_cause_view_source
  ON cache_cause (view_source_id);
CREATE INDEX IF NOT EXISTS idx_cache_cause_run
  ON cache_cause (run_id);

-- ------------------------------------------------------- step 2 guard -------
-- Must return 0 before step 2. A non-zero count means unbackfilled rows exist
-- and promoting view_source_id into the PK would fail on NOT NULL.
--   SELECT COUNT(*) AS unbackfilled FROM cache_cause WHERE view_source_id IS NULL;
--
-- Must also return 0: rows that would still collide after the key change.
--   SELECT brand, view_type, source, measure, market_id, COUNT(DISTINCT view_source_id) AS markets
--   FROM cache_cause
--   GROUP BY brand, view_type, source, measure, market_id
--   HAVING markets > 1;

-- ---------------------------------------------------------------- step 2 ----
-- Blocked on the guards above and on a separate PL approval.
--
-- ALTER TABLE cache_cause
--   MODIFY COLUMN view_source_id VARCHAR(32) NOT NULL,
--   DROP PRIMARY KEY,
--   ADD PRIMARY KEY (brand, view_type, source, measure, market_id, view_source_id);
