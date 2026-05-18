-- ========================================================================
-- 001 Layer 1 raw tables
-- ========================================================================

USE jw_mart;

-- ------------------------------------------------------------------------
-- UBIST monthly sales (53 xlsx integrated base)
-- ------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ubist_monthly_sales_raw (
  id              BIGINT AUTO_INCREMENT PRIMARY KEY,
  source_folder   VARCHAR(255) NOT NULL  COMMENT 'data/UBIST/Sales (2021-2026.02) etc.',
  source_file     VARCHAR(255) NOT NULL,
  sheet_name      VARCHAR(255) NOT NULL,
  source_row_no   INT NOT NULL           COMMENT 'Workbook row number',
  detected_channel   VARCHAR(64)         COMMENT 'UBIST raw channel label',
  detected_specialty VARCHAR(64)         COMMENT 'UBIST raw specialty label',
  period_yyyymm   CHAR(7) NOT NULL       COMMENT 'Example: 2026.04',
  payload         JSON NOT NULL          COMMENT 'Full workbook row payload',
  source_master_version VARCHAR(255),
  ingested_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

  _period_yyyy_text CHAR(4)
    AS (SUBSTRING(period_yyyymm, 1, 4)) VIRTUAL INVISIBLE,
  period_yyyy SMALLINT
    AS (_period_yyyy_text) VIRTUAL,
  _period_mm_text CHAR(2)
    AS (SUBSTRING(period_yyyymm, 6, 2)) VIRTUAL INVISIBLE,
  period_mm   TINYINT
    AS (_period_mm_text) VIRTUAL,

  INDEX idx_period_yyyymm (period_yyyymm),
  INDEX idx_period_yyyy (period_yyyy),
  INDEX idx_channel (detected_channel),
  INDEX idx_specialty (detected_specialty),
  INDEX idx_source_file (source_file(64))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ------------------------------------------------------------------------
-- IQVIA NSA (quarterly sales)
-- ------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS iqvia_nsa_quarterly_raw (
  id              BIGINT AUTO_INCREMENT PRIMARY KEY,
  source_file     VARCHAR(255) NOT NULL,
  sheet_name      VARCHAR(255),
  source_row_no   INT NOT NULL,
  audit_code      VARCHAR(64)            COMMENT 'IQVIA AUDIT CODE',
  audit_desc      VARCHAR(512),
  mfr_code        VARCHAR(64),
  mfr_name        VARCHAR(255),
  period_yyyy     SMALLINT,
  period_quarter  TINYINT                COMMENT '1, 2, 3, 4',
  period_label    VARCHAR(32)            COMMENT 'Example: Q1 2025',
  payload         JSON NOT NULL,
  source_master_version VARCHAR(255),
  ingested_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

  INDEX idx_audit_code (audit_code),
  INDEX idx_period (period_yyyy, period_quarter),
  INDEX idx_mfr (mfr_code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ------------------------------------------------------------------------
-- IQVIA CSD (monthly channel/keyword/meeting)
-- ------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS iqvia_csd_monthly_raw (
  id              BIGINT AUTO_INCREMENT PRIMARY KEY,
  source_file     VARCHAR(255) NOT NULL,
  sheet_name      VARCHAR(255),
  source_row_no   INT NOT NULL,
  period_yyyymm   CHAR(7),
  channel         VARCHAR(64)            COMMENT 'KHPA/KCPA/KPA etc.',
  region          VARCHAR(64),
  payload         JSON NOT NULL,
  source_master_version VARCHAR(255),
  ingested_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

  INDEX idx_period (period_yyyymm),
  INDEX idx_channel (channel),
  INDEX idx_region (region)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ------------------------------------------------------------------------
-- IQVIA CHSO (monthly sellout)
-- ------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS iqvia_chso_monthly_raw (
  id              BIGINT AUTO_INCREMENT PRIMARY KEY,
  source_file     VARCHAR(255) NOT NULL,
  sheet_name      VARCHAR(255),
  source_row_no   INT NOT NULL,
  period_yyyymm   CHAR(7),
  payload         JSON NOT NULL,
  source_master_version VARCHAR(255),
  ingested_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

  INDEX idx_period (period_yyyymm)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
