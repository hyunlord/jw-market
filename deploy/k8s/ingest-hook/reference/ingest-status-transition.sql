-- Activation-time DDL only. Do not run as part of image startup or Job execution.
CREATE TABLE IF NOT EXISTS ingest_status_transition (
  id              BIGINT AUTO_INCREMENT PRIMARY KEY,
  event_id        CHAR(36) NOT NULL,
  epoch           VARCHAR(32) NOT NULL,
  category        VARCHAR(32) NOT NULL,
  manifest_sha    CHAR(64) NOT NULL,
  previous_status VARCHAR(16) NULL,
  status          VARCHAR(16) NOT NULL,
  actor           VARCHAR(64) NOT NULL,
  source          VARCHAR(64) NOT NULL,
  reason          TEXT NULL,
  job_name        VARCHAR(128) NULL,
  evidence_json   LONGTEXT NOT NULL,
  created_at      DATETIME NOT NULL,
  UNIQUE KEY uq_status_transition_event (event_id),
  KEY idx_status_transition_identity (epoch, category, manifest_sha, id)
);
