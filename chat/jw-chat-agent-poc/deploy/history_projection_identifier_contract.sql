ALTER TABLE jw_mart.jw_chat_agent_history_projection_outbox
    ADD COLUMN IF NOT EXISTS source_conversation_id VARCHAR(128) NULL AFTER session_id,
    ADD COLUMN IF NOT EXISTS source_kind VARCHAR(32) NOT NULL DEFAULT 'unknown'
        AFTER source_conversation_id;

UPDATE jw_mart.jw_chat_agent_history_projection_outbox
SET source_conversation_id = session_id
WHERE source_conversation_id IS NULL;

ALTER TABLE jw_mart.jw_chat_agent_history_projection_outbox
    MODIFY COLUMN source_conversation_id VARCHAR(128) NOT NULL;

CREATE TABLE IF NOT EXISTS jw_mart.jw_chat_agent_history_projection_enqueue_failure (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    source_log_id BIGINT UNSIGNED NOT NULL,
    projection_session_id CHAR(36) NULL,
    source_conversation_id VARCHAR(128) NULL,
    source_kind VARCHAR(32) NOT NULL DEFAULT 'unknown',
    error_type VARCHAR(128) NOT NULL,
    error_message VARCHAR(1000) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'enqueue_failed',
    occurrences INT UNSIGNED NOT NULL DEFAULT 1,
    first_failed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_failed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_jw_chat_projection_enqueue_failure_source (source_log_id, status),
    KEY idx_jw_chat_projection_enqueue_failure_status (status, last_failed_at, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
