CREATE TABLE IF NOT EXISTS jw_mart.jw_chat_agent_history_projection_outbox (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    source_log_id BIGINT UNSIGNED NOT NULL,
    session_id CHAR(36) NOT NULL,
    turn_id VARCHAR(64) NOT NULL,
    turn_index INT UNSIGNED NOT NULL,
    projection_version SMALLINT UNSIGNED NOT NULL,
    trace_id VARCHAR(64) NOT NULL,
    span_id VARCHAR(64) NOT NULL,
    portal_user_id INT NULL,
    request_headers_json LONGTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL
        CHECK (JSON_VALID(request_headers_json)),
    payload_json LONGTEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL
        CHECK (JSON_VALID(payload_json)),
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    attempts SMALLINT UNSIGNED NOT NULL DEFAULT 0,
    max_attempts SMALLINT UNSIGNED NOT NULL DEFAULT 5,
    next_attempt_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_error VARCHAR(1000) NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    completed_at DATETIME NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_jw_chat_agent_projection_turn (session_id, turn_id, projection_version),
    UNIQUE KEY uq_jw_chat_agent_projection_source (source_log_id, projection_version),
    KEY idx_jw_chat_agent_projection_poll (status, next_attempt_at, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
