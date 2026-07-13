CREATE TABLE IF NOT EXISTS agent3_brand_strength (
  brand_key VARCHAR(255) NOT NULL,
  brand_name VARCHAR(255) NOT NULL,
  profile_json LONGTEXT NOT NULL CHECK (JSON_VALID(profile_json)),
  strength_candidates_json LONGTEXT NOT NULL CHECK (JSON_VALID(strength_candidates_json)),
  strength_summary_json LONGTEXT NOT NULL CHECK (JSON_VALID(strength_summary_json)),
  workflow_id INT NOT NULL,
  workflow_rev INT NOT NULL,
  input_hash CHAR(64) NOT NULL,
  generated_at DATETIME NOT NULL,
  PRIMARY KEY (brand_key),
  KEY idx_agent3_brand_strength_brand_name (brand_name),
  KEY idx_agent3_brand_strength_hash (input_hash),
  KEY idx_agent3_brand_strength_generated_at (generated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
