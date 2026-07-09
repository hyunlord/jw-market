-- Phase 2: cache_deep_analysis v3 - brand single PK, no query parameters.

DROP TABLE IF EXISTS cache_deep_analysis;
CREATE TABLE cache_deep_analysis (
    brand VARCHAR(255) PRIMARY KEY,
    market_id VARCHAR(20) NOT NULL COMMENT 'strategy_NNN',
    response_json LONGTEXT NOT NULL COMMENT '{ forecast: { by_combo: {...} }, simulation: { by_combo: {...} }, events: [...], ai_analysis: {...} }',
    payload_size INT NOT NULL,
    brand_factors LONGTEXT NULL COMMENT 'Mart-backed factor snapshot for ATC, UBIST, and IQVIA catalog dimensions',
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_market (market_id),
    CHECK (JSON_VALID(response_json)),
    CHECK (brand_factors IS NULL OR JSON_VALID(brand_factors))
);
