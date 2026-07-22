-- Activation-time DDL only. Do not run as part of image startup or Job execution.
CREATE TABLE IF NOT EXISTS ingest_signal_event (
  id              BIGINT AUTO_INCREMENT PRIMARY KEY,
  epoch           VARCHAR(32) NOT NULL,
  category        VARCHAR(32) NOT NULL,
  manifest_sha    CHAR(64) NOT NULL,
  run_id          VARCHAR(64) NOT NULL,
  event           VARCHAR(16) NOT NULL,
  mode            VARCHAR(16) NOT NULL,
  rows_loaded     BIGINT NOT NULL,
  delivery_status VARCHAR(16) NOT NULL,
  attempts        INT NOT NULL,
  reason          TEXT NULL,
  payload_json    LONGTEXT NOT NULL,
  created_at      DATETIME NOT NULL,
  UNIQUE KEY uq_signal_identity (epoch, category, manifest_sha, event),
  KEY idx_signal_lookup (epoch, category, manifest_sha)
);
