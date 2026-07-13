CREATE TABLE IF NOT EXISTS agent3_brand_strength_market (
  brand_key VARCHAR(255) NOT NULL,
  source VARCHAR(16) NOT NULL,
  market_id VARCHAR(32) NOT NULL,
  view_kind VARCHAR(32) NOT NULL,
  brand_name VARCHAR(255) NOT NULL,
  serving_brand_name VARCHAR(255) NULL,
  profile_json LONGTEXT NOT NULL CHECK (JSON_VALID(profile_json)),
  strength_candidates_json LONGTEXT NOT NULL CHECK (JSON_VALID(strength_candidates_json)),
  strength_summary_json LONGTEXT NOT NULL CHECK (JSON_VALID(strength_summary_json)),
  workflow_id INT NOT NULL,
  workflow_rev INT NOT NULL,
  input_hash CHAR(64) NOT NULL,
  generation_status VARCHAR(32) NOT NULL,
  generated_at DATETIME NOT NULL,
  PRIMARY KEY (brand_key, source, market_id),
  KEY idx_market_scope (view_kind, market_id, source),
  KEY idx_serving_market_source (serving_brand_name, market_id, source),
  KEY idx_generation_status (generation_status),
  KEY idx_generated_at (generated_at),
  CONSTRAINT chk_agent3_market_source CHECK (source IN ('iqvia', 'ubist')),
  CONSTRAINT chk_agent3_market_view CHECK (
    (view_kind = 'market_landscape' AND market_id LIKE 'ml\_%') OR
    (view_kind = 'competitive_dynamics' AND market_id LIKE 'cd\_%')
  )
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
