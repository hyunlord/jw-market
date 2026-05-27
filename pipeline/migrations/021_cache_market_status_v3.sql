-- Phase 2: cache_market_status v3 - KPI envelope + 25 brand cards.

DROP TABLE IF EXISTS cache_market_status;
CREATE TABLE cache_market_status (
    query_key VARCHAR(255) PRIMARY KEY COMMENT 'default | market_id=strategy_NNN',
    response_json LONGTEXT NOT NULL COMMENT '{ kpi: { ubist: {...}, iqvia: {...} }, brand_cards: [...] }',
    payload_size INT NOT NULL,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CHECK (JSON_VALID(response_json))
);
