-- Phase ζ trace tables. Stage 3-C applies this migration before the Gemini dry-test.

CREATE TABLE IF NOT EXISTS zeta_analysis_runs (
  run_id          BIGINT AUTO_INCREMENT PRIMARY KEY,
  brand           VARCHAR(255) NOT NULL,
  snapshot_at     DATETIME NOT NULL,
  config_version  VARCHAR(64) NOT NULL,
  builder_version VARCHAR(64) NOT NULL,
  bundle_hash     VARCHAR(80) NOT NULL,
  model_version   VARCHAR(64),
  status          VARCHAR(32) NOT NULL,
  total_tokens_in INT,
  total_tokens_out INT,
  cost_usd        DECIMAL(10, 6),
  duration_sec    DECIMAL(8, 2),
  input_bundle    LONGTEXT,
  error_log       TEXT,
  created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_brand_snapshot (brand, snapshot_at),
  KEY idx_bundle_hash (bundle_hash),
  KEY idx_status (status),
  KEY idx_brand (brand)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS zeta_analysis_outputs (
  output_id       BIGINT AUTO_INCREMENT PRIMARY KEY,
  run_id          BIGINT NOT NULL,
  stage           VARCHAR(20) NOT NULL,
  title           VARCHAR(500),
  body            TEXT,
  bullets         JSON,
  raw_response    LONGTEXT,
  validated       TINYINT(1) DEFAULT 0,
  validation_log  TEXT,
  tokens_in       INT,
  tokens_out      INT,
  created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY (run_id) REFERENCES zeta_analysis_runs(run_id) ON DELETE CASCADE,
  UNIQUE KEY uq_run_stage (run_id, stage)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
