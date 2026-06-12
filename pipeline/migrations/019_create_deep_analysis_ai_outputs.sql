-- Phase B-1 Agent 2 Deep Analysis
-- Store validated 4-stage Agent 2 output per brand and run timestamp.

CREATE TABLE IF NOT EXISTS deep_analysis_ai_outputs (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  brand_canonical VARCHAR(255) NOT NULL,
  brand_id VARCHAR(64),
  ml_id VARCHAR(20),
  generated_at DATETIME NOT NULL,
  agent_version VARCHAR(20),
  phenomenon JSON,
  cause JSON,
  prediction JSON,
  recommendation JSON,
  validation_status VARCHAR(20),
  validation_log JSON,
  input_event_ids JSON,
  input_metric_hash VARCHAR(64),
  llm_model VARCHAR(100),
  llm_input_tokens INT,
  llm_output_tokens INT,
  llm_cost_usd DECIMAL(8,4),
  UNIQUE KEY uq_deep_ai_brand_generated (brand_canonical, generated_at),
  INDEX idx_deep_ai_brand (brand_canonical),
  INDEX idx_deep_ai_validation (validation_status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
