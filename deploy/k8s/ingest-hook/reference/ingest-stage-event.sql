-- Activation-time DDL only. Do not run as part of image startup or Job execution.
CREATE TABLE IF NOT EXISTS ingest_stage_event (
  id            BIGINT AUTO_INCREMENT PRIMARY KEY,
  epoch         VARCHAR(32)  NOT NULL,
  category      VARCHAR(32)  NOT NULL,
  manifest_sha  CHAR(64)     NOT NULL,
  run_id        VARCHAR(64)  NOT NULL,
  seq           INT          NOT NULL,
  stage         VARCHAR(32)  NOT NULL,
  status        VARCHAR(16)  NOT NULL,
  reason        TEXT         NULL,
  started_at    DATETIME     NULL,
  finished_at   DATETIME     NULL,
  duration_ms   BIGINT       NULL,
  UNIQUE KEY uq_stage_identity (epoch, category, manifest_sha, run_id, seq),
  KEY idx_stage_lookup (epoch, category, manifest_sha)
);
