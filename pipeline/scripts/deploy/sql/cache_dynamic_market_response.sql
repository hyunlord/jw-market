CREATE TABLE IF NOT EXISTS cache_dynamic_market_response (
    cache_key CHAR(64) NOT NULL,
    request_json LONGTEXT NOT NULL CHECK (JSON_VALID(request_json)),
    source_epoch CHAR(64) NOT NULL,
    state ENUM('building', 'ready', 'failed') NOT NULL,
    lease_owner VARCHAR(64) NULL,
    lease_expires_at DATETIME NULL,
    response_json LONGTEXT NULL CHECK (response_json IS NULL OR JSON_VALID(response_json)),
    response_sha256 CHAR(64) NULL,
    payload_size INT UNSIGNED NULL,
    expires_at DATETIME NULL,
    hit_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
    last_hit_at DATETIME NULL,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    PRIMARY KEY (cache_key),
    KEY idx_dynamic_response_expiry (state, expires_at),
    KEY idx_dynamic_response_lease (state, lease_expires_at)
) CHARACTER SET utf8mb4 COLLATE utf8mb4_uca1400_ai_ci;
