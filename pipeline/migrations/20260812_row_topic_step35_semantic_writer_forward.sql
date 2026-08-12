-- ROW_TOPIC_STEP3_5_IMPL_20260812: approved forward DDL.
-- Timestamps are UTC naive and application supplied.

CREATE TABLE jw_brand_activity_stage.row_topic_assignment_run_semantic_v1 (
  run_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  release_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  stage_generation_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  prompt_version VARCHAR(64) NOT NULL,
  execution_mode VARCHAR(16) NOT NULL,
  status VARCHAR(32) NOT NULL,
  planned_occurrences INT UNSIGNED NOT NULL,
  planned_calls INT UNSIGNED NOT NULL,
  calls_used INT UNSIGNED NOT NULL DEFAULT 0,
  failed_batches INT UNSIGNED NOT NULL DEFAULT 0,
  started_at DATETIME(6) NOT NULL COMMENT 'UTC naive; application supplied',
  finished_at DATETIME(6) NULL COMMENT 'UTC naive; application supplied',
  created_by VARCHAR(255) NOT NULL,
  PRIMARY KEY (run_id),
  KEY idx_rtasr_v1_release (release_id, status),
  KEY idx_rtasr_v1_generation (stage_generation_id, started_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE jw_brand_activity_stage.row_topic_assignment_batch_semantic_v1 (
  run_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  batch_id VARCHAR(192) NOT NULL,
  wave_no SMALLINT UNSIGNED NOT NULL,
  scope_id VARCHAR(128) NOT NULL,
  brand VARCHAR(255) NOT NULL,
  occurrence_count INT UNSIGNED NOT NULL,
  semantic_key_count INT UNSIGNED NOT NULL,
  occurrence_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  status VARCHAR(32) NOT NULL,
  calls_used INT UNSIGNED NOT NULL DEFAULT 0,
  error_code VARCHAR(64) NULL,
  error_message VARCHAR(1000) NULL,
  started_at DATETIME(6) NULL COMMENT 'UTC naive; application supplied',
  finished_at DATETIME(6) NULL COMMENT 'UTC naive; application supplied',
  PRIMARY KEY (run_id, batch_id),
  KEY idx_rtabs_v1_wave_status (run_id, wave_no, status),
  KEY idx_rtabs_v1_scope_brand (scope_id, brand)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE jw_brand_activity_stage.row_topic_taxonomy_release_v1 (
  release_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  manifest_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  stage_generation_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  status VARCHAR(32) NOT NULL,
  expected_scope_count SMALLINT UNSIGNED NOT NULL,
  semantic_scope_count SMALLINT UNSIGNED NOT NULL,
  legacy_scope_count SMALLINT UNSIGNED NOT NULL,
  created_at DATETIME(6) NOT NULL COMMENT 'UTC naive; application supplied',
  created_by VARCHAR(255) NOT NULL,
  ready_at DATETIME(6) NULL COMMENT 'UTC naive; application supplied',
  PRIMARY KEY (release_id),
  KEY idx_rttr_v1_status_created (status, created_at),
  KEY idx_rttr_v1_generation (stage_generation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE jw_brand_activity_stage.row_topic_taxonomy_release_manifest_v1 (
  release_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  scope_id VARCHAR(128) NOT NULL,
  topic_set_version VARCHAR(128) NOT NULL,
  assignment_contract VARCHAR(32) NOT NULL,
  stage_generation_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
  created_at DATETIME(6) NOT NULL COMMENT 'UTC naive; application supplied',
  PRIMARY KEY (release_id, scope_id),
  KEY idx_rttrm_v1_scope_version (scope_id, topic_set_version),
  KEY idx_rttrm_v1_version (topic_set_version),
  KEY idx_rttrm_v1_generation (stage_generation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE jw_brand_activity_stage.row_topic_taxonomy_active_release_v1 (
  pointer_name VARCHAR(64) NOT NULL,
  active_release_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin NULL,
  generation BIGINT UNSIGNED NOT NULL DEFAULT 0,
  updated_at DATETIME(6) NOT NULL COMMENT 'UTC naive; application supplied',
  updated_by VARCHAR(255) NOT NULL,
  PRIMARY KEY (pointer_name),
  KEY idx_rttar_v1_release (active_release_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
