-- Phase 2: cache_cause v3 - brand x view x source x measure, widened by market_id.
--
-- The public lookup grain is brand/view/source/measure. The storage key also
-- includes market_id because Phase 1 marts contain non-JW market-member brands
-- with the same display name in multiple strategy markets. Keeping market_id in
-- the key preserves the requested mart 1:1 rebuild without dropping collisions.

DROP TABLE IF EXISTS cache_cause;
CREATE TABLE cache_cause (
    brand VARCHAR(255) NOT NULL,
    view_type VARCHAR(30) NOT NULL COMMENT 'market_landscape | competitive_dynamics',
    source VARCHAR(10) NOT NULL COMMENT 'UBIST | IQVIA',
    measure VARCHAR(20) NOT NULL COMMENT 'sales | volume | unit | dosage_unit | counting_unit',
    market_id VARCHAR(20) NOT NULL COMMENT 'strategy_NNN',
    response_json LONGTEXT NOT NULL,
    payload_size INT NOT NULL,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (brand, view_type, source, measure, market_id),
    INDEX idx_brand (brand),
    INDEX idx_market (market_id),
    CHECK (JSON_VALID(response_json))
);
