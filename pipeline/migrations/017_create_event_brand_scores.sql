-- Phase B-1 Agent 2 Deep Analysis
-- N:M event-to-brand scoring table from corpus matches[].

CREATE TABLE IF NOT EXISTS event_brand_scores (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  event_id VARCHAR(64) NOT NULL,
  brand_name VARCHAR(255) NOT NULL,
  brand_canonical VARCHAR(255),
  brand_id VARCHAR(64),
  ml_id VARCHAR(20),
  cd_id VARCHAR(20),
  is_jw TINYINT DEFAULT 0,
  score INT NOT NULL CHECK (score >= 0 AND score <= 100),
  score_tier VARCHAR(30),
  reason TEXT,
  source_processor VARCHAR(50),
  generated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_event_brand (event_id, brand_canonical),
  INDEX idx_event_brand_scores_brand_score (brand_canonical, score DESC),
  INDEX idx_event_brand_scores_ml_id (ml_id),
  CONSTRAINT fk_event_brand_scores_events
    FOREIGN KEY (event_id) REFERENCES events(event_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
