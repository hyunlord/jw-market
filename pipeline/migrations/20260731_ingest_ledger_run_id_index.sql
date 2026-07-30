-- Apply with jw_mart_d2_stage_20260630_r2 selected as the current database.
-- The online DDL keeps ledger reads and writes available while adding the
-- deterministic post-gate lookup index.
ALTER TABLE ingest_ledger
  ADD INDEX IF NOT EXISTS idx_ledger_run_id_id (run_id, id),
  ALGORITHM=INPLACE,
  LOCK=NONE;
