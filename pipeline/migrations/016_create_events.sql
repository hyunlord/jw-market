-- Phase B-1 Agent 2 Deep Analysis
-- Normalize one processed news article into one market event.

CREATE TABLE IF NOT EXISTS events (
  event_id VARCHAR(64) PRIMARY KEY,
  news_id VARCHAR(64) NOT NULL,
  category VARCHAR(50),
  category_label VARCHAR(50),
  date DATE,
  title TEXT,
  summary TEXT,
  body_full LONGTEXT,
  source_name VARCHAR(50),
  source_url TEXT,
  period_ubist VARCHAR(20),
  period_iqvia VARCHAR(20),
  processed_by VARCHAR(50),
  processed_at DATETIME,
  search_keyword VARCHAR(255),
  INDEX idx_events_category_date (category, date),
  INDEX idx_events_date (date),
  CONSTRAINT fk_events_news_raw
    FOREIGN KEY (news_id) REFERENCES news_raw(news_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
