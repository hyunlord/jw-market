-- ============================================================
-- Phase 16-C-2 architecture change (v9 hybrid)
--   UBIST: MariaDB raw -> parquet hive partition
--   IQVIA NSA / CSD / CHSO: MariaDB raw (unchanged)
-- ============================================================

USE jw_mart;

DROP TABLE IF EXISTS ubist_monthly_sales_raw;
