-- Durable axis-to-assignment receipt consumed by row_topic_monthly_wrapper.py.
CREATE TABLE IF NOT EXISTS `jw_brand_activity_stage`.`mart_brand_activity_assignment_handoff` (
  run_id VARCHAR(160) NOT NULL,
  target_mode VARCHAR(32) NOT NULL,
  input_fingerprint CHAR(64) NOT NULL,
  expected_scope_count INT NOT NULL,
  stored_scope_count INT NOT NULL,
  scope_identity_sha256 CHAR(64) NOT NULL,
  assignment_population_count BIGINT NOT NULL,
  assignment_population_sha256 CHAR(64) NOT NULL,
  axis_status VARCHAR(32) NOT NULL,
  assignment_status VARCHAR(32) NOT NULL,
  last_error VARCHAR(512) NOT NULL DEFAULT '',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (run_id),
  KEY idx_topic_assignment_handoff_pending (axis_status, assignment_status, created_at, run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Rollback (record only; never run automatically):
-- DROP TABLE `jw_brand_activity_stage`.`mart_brand_activity_assignment_handoff`;
