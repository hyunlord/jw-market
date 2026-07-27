-- Activation-time DDL only. Do not run as part of image startup or Job execution.
CREATE TABLE IF NOT EXISTS ingest_ledger (
  id            BIGINT AUTO_INCREMENT PRIMARY KEY,
  epoch         VARCHAR(32)  NOT NULL,
  category      VARCHAR(32)  NOT NULL,
  manifest_sha  CHAR(64)     NOT NULL,
  manifest_path VARCHAR(512) NOT NULL,
  uploaded_by   VARCHAR(128) NULL,
  status        VARCHAR(16)  NOT NULL,
  reason        TEXT         NULL,
  job_name      VARCHAR(128) NULL,
  run_id        VARCHAR(64)  NULL,
  row_counts    TEXT         NULL,
  received_at   DATETIME     NOT NULL,
  started_at    DATETIME     NULL,
  finished_at   DATETIME     NULL,
  UNIQUE KEY uq_ledger_identity (epoch, category, manifest_sha),
  KEY idx_ledger_category_status (category, status)
);
