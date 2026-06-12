-- Phase B-1 Agent 2 Deep Analysis
-- Track corpus loader and Agent 2 batch runs.

CREATE TABLE IF NOT EXISTS agent_run_log (
  run_id BIGINT AUTO_INCREMENT PRIMARY KEY,
  agent_name VARCHAR(50) NOT NULL,
  agent_version VARCHAR(20),
  started_at DATETIME,
  finished_at DATETIME,
  status VARCHAR(20),
  input_count INT,
  output_count INT,
  skipped_count INT,
  error_count INT,
  llm_input_tokens BIGINT DEFAULT 0,
  llm_output_tokens BIGINT DEFAULT 0,
  llm_cost_usd DECIMAL(10,4) DEFAULT 0,
  llm_model VARCHAR(100),
  notes TEXT,
  INDEX idx_agent_run_log_agent_started (agent_name, started_at DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
