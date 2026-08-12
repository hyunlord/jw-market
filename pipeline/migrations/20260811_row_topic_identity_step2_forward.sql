CREATE TABLE jw_brand_activity_stage.row_topic_stage_occurrence_v1 (
  stage_generation_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  stage_row_id BIGINT UNSIGNED NOT NULL,
  semantic_event_key_v1 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  stage_row_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  source_file VARCHAR(255) NOT NULL,
  source_sheet VARCHAR(64) NOT NULL,
  source_row_no INT NOT NULL,
  source_file_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  backfill_batch_id VARCHAR(192) NOT NULL,
  backfilled_at DATETIME(6) NOT NULL COMMENT 'UTC naive; application supplied',
  PRIMARY KEY (stage_generation_id, stage_row_id),
  UNIQUE KEY uq_rtso_v1_generation_lineage
    (stage_generation_id, source_file_sha256, source_sheet, source_row_no),
  KEY idx_rtso_v1_generation_semantic
    (stage_generation_id, semantic_event_key_v1, stage_row_id),
  KEY idx_rtso_v1_semantic_generation
    (semantic_event_key_v1, stage_generation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE jw_brand_activity_stage.row_topic_assignment_semantic_v1 (
  semantic_event_key_v1 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  scope_id VARCHAR(128) NOT NULL,
  brand VARCHAR(255) NOT NULL,
  topic_id VARCHAR(128) NOT NULL,
  topic_set_version VARCHAR(128) NOT NULL,
  prompt_version VARCHAR(64) NOT NULL,
  assigned_at DATETIME(6) NOT NULL COMMENT 'UTC naive; application supplied',
  batch_id VARCHAR(192) NOT NULL,
  PRIMARY KEY (semantic_event_key_v1, scope_id, topic_set_version, topic_id),
  KEY idx_rtas_v1_scope_brand_version (scope_id, brand, topic_set_version),
  KEY idx_rtas_v1_topic_version (topic_id, topic_set_version),
  KEY idx_rtas_v1_batch (batch_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE jw_brand_activity_stage.row_topic_assignment_status_semantic_v1 (
  semantic_event_key_v1 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  scope_id VARCHAR(128) NOT NULL,
  topic_set_version VARCHAR(128) NOT NULL,
  classified_stage_generation_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  prompt_version VARCHAR(64) NOT NULL,
  batch_id VARCHAR(192) NOT NULL,
  status VARCHAR(32) NOT NULL,
  assignment_count INT NOT NULL DEFAULT 0,
  classified_at DATETIME(6) NOT NULL COMMENT 'UTC naive; application supplied',
  PRIMARY KEY (semantic_event_key_v1, scope_id, topic_set_version),
  KEY idx_rtass_v1_version_status (topic_set_version, status),
  KEY idx_rtass_v1_batch (batch_id),
  KEY idx_rtass_v1_generation (classified_stage_generation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
