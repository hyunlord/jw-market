CREATE TABLE IF NOT EXISTS cache_deep_analysis_ai_analysis (
    brand VARCHAR(255) NOT NULL,
    market_id VARCHAR(20),
    ai_analysis_json LONGTEXT,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (brand)
);
