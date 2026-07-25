CREATE TABLE IF NOT EXISTS hira_benefit_notice (
  source_notice_id VARCHAR(32) NOT NULL,
  source_url VARCHAR(1024) NOT NULL,
  title VARCHAR(1024) DEFAULT NULL,
  notice_no VARCHAR(128) DEFAULT NULL,
  notice_date DATE DEFAULT NULL,
  target_condition LONGTEXT DEFAULT NULL,
  exclusion_rule LONGTEXT DEFAULT NULL,
  dosage_limit LONGTEXT DEFAULT NULL,
  raw_text LONGTEXT NOT NULL,
  raw_html_sha256 CHAR(64) NOT NULL,
  listing_fingerprint CHAR(64) NOT NULL,
  parse_status VARCHAR(16) NOT NULL,
  parse_failed_fields_json LONGTEXT NOT NULL,
  collected_at DATETIME(6) NOT NULL,
  updated_at DATETIME(6) NOT NULL,
  PRIMARY KEY (source_notice_id),
  KEY idx_hira_notice_date (notice_date),
  KEY idx_hira_collected_at (collected_at),
  KEY idx_hira_parse_status (parse_status),
  CONSTRAINT chk_hira_parse_failed_fields
    CHECK (JSON_VALID(parse_failed_fields_json))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS hira_benefit_notice_brand (
  source_notice_id VARCHAR(32) NOT NULL,
  brand_name VARCHAR(255) NOT NULL,
  brand_key VARCHAR(255) DEFAULT NULL,
  match_method VARCHAR(32) NOT NULL,
  created_at DATETIME(6) NOT NULL,
  PRIMARY KEY (source_notice_id, brand_name),
  KEY idx_hira_brand_name (brand_name, source_notice_id),
  KEY idx_hira_brand_key (brand_key, source_notice_id),
  CONSTRAINT fk_hira_notice_brand_notice
    FOREIGN KEY (source_notice_id)
    REFERENCES hira_benefit_notice (source_notice_id)
    ON UPDATE CASCADE ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS hira_benefit_crawl_run (
  run_id VARCHAR(128) NOT NULL,
  started_at DATETIME(6) NOT NULL,
  finished_at DATETIME(6) NOT NULL,
  exit_code INT NOT NULL,
  failures INT NOT NULL,
  identity_gap INT NOT NULL,
  pending_gap INT NOT NULL,
  parsed_count INT NOT NULL,
  partial_count INT NOT NULL,
  failed_count INT NOT NULL,
  status VARCHAR(16) NOT NULL,
  alert_status VARCHAR(16) DEFAULT NULL,
  receipt_json LONGTEXT NOT NULL,
  PRIMARY KEY (run_id),
  KEY idx_hira_run_finished (finished_at),
  KEY idx_hira_run_status (status, finished_at),
  CONSTRAINT chk_hira_run_receipt_json CHECK (JSON_VALID(receipt_json))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS hira_benefit_crawl_state (
  source_key VARCHAR(64) NOT NULL,
  last_success_run_id VARCHAR(128) NOT NULL,
  last_success_at DATETIME(6) NOT NULL,
  last_seen_notice_id VARCHAR(32) DEFAULT NULL,
  index_tag_signature_sha256 CHAR(64) NOT NULL,
  mapping_revision VARCHAR(128) NOT NULL,
  receipt_json LONGTEXT NOT NULL,
  PRIMARY KEY (source_key),
  CONSTRAINT chk_hira_receipt_json CHECK (JSON_VALID(receipt_json))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
