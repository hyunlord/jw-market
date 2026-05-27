-- Phase 2: cache_brands v3 - query-key cache, 25 canonical brands.

DROP TABLE IF EXISTS cache_brands;
CREATE TABLE cache_brands (
    query_key VARCHAR(255) PRIMARY KEY COMMENT 'default | q=<text> | market_id=strategy_NNN | q=<text>&market_id=strategy_NNN',
    response_json LONGTEXT NOT NULL,
    payload_size INT NOT NULL,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    CHECK (JSON_VALID(response_json))
);
