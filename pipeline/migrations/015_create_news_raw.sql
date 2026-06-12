-- Phase B-1 Agent 2 Deep Analysis
-- Store processed news corpus provenance and article text.

CREATE TABLE IF NOT EXISTS news_raw (
  news_id VARCHAR(64) PRIMARY KEY,
  source_name VARCHAR(50) NOT NULL,
  title TEXT NOT NULL,
  article_url TEXT,
  article_text LONGTEXT,
  raw_html LONGTEXT,
  published_date DATE,
  search_keyword VARCHAR(255),
  ingested_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  corpus_file_path VARCHAR(1000),
  INDEX idx_news_raw_published_date (published_date),
  INDEX idx_news_raw_source_name (source_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
