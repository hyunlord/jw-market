CREATE TABLE IF NOT EXISTS `jw_market_audit_stage`.`report_download_event` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `actor_uid` VARCHAR(128) NULL,
    `actor_type` ENUM('user', 'service', 'unknown', 'system') NOT NULL,
    `completed_at` DATETIME(6) NOT NULL,
    `report_type` VARCHAR(64) NOT NULL,
    `report_id` VARCHAR(128) NOT NULL,
    `completion_stage` ENUM('upstream_response', 'browser_payload_ready') NOT NULL,
    `success` BOOLEAN NOT NULL,
    `trace_id` VARCHAR(128) NULL,
    `jti` VARCHAR(64) NULL,
    PRIMARY KEY (`id`),
    KEY `idx_report_download_completed_at` (`completed_at`),
    KEY `idx_report_download_actor_completed_at` (`actor_uid`, `completed_at`),
    KEY `idx_report_download_trace_id` (`trace_id`)
) ENGINE=InnoDB;

CREATE OR REPLACE SQL SECURITY DEFINER VIEW
    `jw_market_audit_stage`.`dashboard_api_usage_v` AS
SELECT
    `id`, `actor_uid`, `actor_type`, `called_at`, `endpoint`, `http_status`,
    JSON_OBJECT(
        'path', JSON_OBJECT(
            'brand_name', JSON_EXTRACT(`request_params`, '$.path.brand_name')
        ),
        'query', JSON_OBJECT(
            'market_id', JSON_EXTRACT(`request_params`, '$.query.market_id'),
            'view', JSON_EXTRACT(`request_params`, '$.query.view'),
            'source', JSON_EXTRACT(`request_params`, '$.query.source'),
            'measure', JSON_EXTRACT(`request_params`, '$.query.measure'),
            'brand', JSON_EXTRACT(`request_params`, '$.query.brand'),
            'brand_name', JSON_EXTRACT(`request_params`, '$.query.brand_name'),
            'atc4_codes', JSON_EXTRACT(`request_params`, '$.query.atc4_codes')
        ),
        'body', JSON_OBJECT(
            'view', JSON_EXTRACT(`request_params`, '$.body.view'),
            'source', JSON_EXTRACT(`request_params`, '$.body.source'),
            'measure', JSON_EXTRACT(`request_params`, '$.body.measure'),
            'selected_brand', JSON_EXTRACT(`request_params`, '$.body.selected_brand'),
            'period', JSON_EXTRACT(`request_params`, '$.body.period'),
            'period_start', JSON_EXTRACT(`request_params`, '$.body.period_start'),
            'period_end', JSON_EXTRACT(`request_params`, '$.body.period_end'),
            'csd_channel', JSON_EXTRACT(`request_params`, '$.body.csd_channel'),
            'mode', JSON_EXTRACT(`request_params`, '$.body.mode'),
            'specialty', JSON_EXTRACT(`request_params`, '$.body.specialty'),
            'visit_location', JSON_EXTRACT(`request_params`, '$.body.visit_location'),
            'top_n', JSON_EXTRACT(`request_params`, '$.body.top_n'),
            'filters', JSON_OBJECT(
                'analysis_level', JSON_EXTRACT(`request_params`, '$.body.filters.analysis_level'),
                'atc', JSON_EXTRACT(`request_params`, '$.body.filters.atc'),
                'atc4', JSON_EXTRACT(`request_params`, '$.body.filters.atc4'),
                'channel', JSON_EXTRACT(`request_params`, '$.body.filters.channel'),
                'focus_brand_key', JSON_EXTRACT(`request_params`, '$.body.filters.focus_brand_key'),
                'view_kind', JSON_EXTRACT(`request_params`, '$.body.filters.view_kind')
            ),
            'options', JSON_OBJECT(
                'period_range', JSON_EXTRACT(`request_params`, '$.body.options.period_range')
            )
        )
    ) AS `request_options`
FROM `jw_market_audit_stage`.`audit_api_call_log`;

CREATE OR REPLACE SQL SECURITY DEFINER VIEW
    `jw_market_audit_stage`.`dashboard_report_download_v` AS
SELECT
    `id`, `actor_uid`, `actor_type`, `completed_at`, `report_type`,
    `report_id`, `completion_stage`, `success`, `trace_id`
FROM `jw_market_audit_stage`.`report_download_event`;

CREATE OR REPLACE SQL SECURITY DEFINER VIEW
    `jw_market_audit_stage`.`dashboard_chat_usage_v` AS
SELECT
    c.`id` AS `conversation_log_id`,
    o.`portal_user_id`,
    c.`conversation_id`,
    c.`turn_index`,
    c.`contract_status`,
    c.`quality_label`,
    c.`elapsed_ms`,
    s.`service_id`,
    o.`trace_id`,
    JSON_UNQUOTE(JSON_EXTRACT(o.`request_headers_json`, '$."x-request-id"')) AS `request_id`,
    CAST(COALESCE(JSON_UNQUOTE(JSON_EXTRACT(c.`trace_json`, '$.token_usage.available')), 'false') AS CHAR(5)) AS `token_usage_available`,
    CAST(COALESCE(JSON_UNQUOTE(JSON_EXTRACT(c.`trace_json`, '$.token_usage.total_input_tokens')), '0') AS UNSIGNED) AS `input_tokens`,
    CAST(COALESCE(JSON_UNQUOTE(JSON_EXTRACT(c.`trace_json`, '$.token_usage.total_output_tokens')), '0') AS UNSIGNED) AS `output_tokens`,
    CAST(COALESCE(JSON_UNQUOTE(JSON_EXTRACT(c.`trace_json`, '$.token_usage.total_tokens')), '0') AS UNSIGNED) AS `total_tokens`,
    c.`created_at`
FROM `jw_mart`.`jw_chat_agent_conversation_log` c
LEFT JOIN `jw_mart`.`jw_chat_agent_history_projection_outbox` o
    ON o.`source_log_id` = c.`id` AND o.`projection_version` = 1
LEFT JOIN (
    SELECT `uid`,
           CASE WHEN COUNT(DISTINCT `chat_service_id`) = 1
                THEN MAX(`chat_service_id`) ELSE NULL END AS `service_id`
    FROM `llmops`.`chat_session_tb`
    GROUP BY `uid`
) s ON s.`uid` = o.`session_id`;

CREATE OR REPLACE SQL SECURITY DEFINER VIEW
    `jw_market_audit_stage`.`dashboard_chat_turn_detail_v` AS
SELECT
    COALESCE(o.`trace_id`, CONCAT('conversation-log:', c.`id`)) AS `detail_key`,
    c.`question_text`,
    c.`answer_text`
FROM `jw_mart`.`jw_chat_agent_conversation_log` c
LEFT JOIN `jw_mart`.`jw_chat_agent_history_projection_outbox` o
    ON o.`source_log_id` = c.`id` AND o.`projection_version` = 1
UNION ALL
SELECT
    r.`trace_id` AS `detail_key`,
    r.`question_text`,
    r.`answer_text`
FROM `jw_mart`.`rnd_trace_conversation_log` r
WHERE r.`source_system` = 'genos_monitoring' AND r.`service_id` = 61;

CREATE OR REPLACE SQL SECURITY DEFINER VIEW
    `jw_market_audit_stage`.`dashboard_user_directory_v` AS
SELECT `id`, `user_id`, `name`, `department`, `group_name`
FROM `llmops`.`user_tb`
WHERE `is_active` = 1 AND `is_deleted` = 0;

CREATE OR REPLACE SQL SECURITY DEFINER VIEW
    `jw_market_audit_stage`.`dashboard_auth_event_v` AS
SELECT `id`, `user_id`, `type_code`, `reg_date`
FROM `llmops`.`user_auth_log_tb`;

CREATE OR REPLACE SQL SECURITY DEFINER VIEW
    `jw_market_audit_stage`.`dashboard_credit_usage_v` AS
SELECT
    `id`, `user_id`, `reg_date`, `trace_id`, `service_type`, `service_id`,
    `service_revision_id`, `chat_service_id`, `chat_service_revision_id`,
    `workflow_id`, `workflow_revision_id`, `input_token_count`,
    `output_token_count`, `charge`, `overused_charge`, `applied`
FROM `llmops`.`credit_update_history`;

GRANT SELECT ON `jw_market_audit_stage`.`dashboard_api_usage_v`
    TO 'jw_market_audit_reader_stage'@'10.%';
GRANT SELECT ON `jw_market_audit_stage`.`dashboard_report_download_v`
    TO 'jw_market_audit_reader_stage'@'10.%';
GRANT SELECT ON `jw_market_audit_stage`.`dashboard_chat_usage_v`
    TO 'jw_market_audit_reader_stage'@'10.%';
GRANT SELECT ON `jw_market_audit_stage`.`dashboard_chat_turn_detail_v`
    TO 'jw_market_audit_reader_stage'@'10.%';
GRANT SELECT ON `jw_market_audit_stage`.`dashboard_auth_event_v`
    TO 'jw_market_audit_reader_stage'@'10.%';
GRANT SELECT ON `jw_market_audit_stage`.`dashboard_credit_usage_v`
    TO 'jw_market_audit_reader_stage'@'10.%';
GRANT SELECT ON `jw_market_audit_stage`.`dashboard_user_directory_v`
    TO 'jw_market_audit_reader_stage'@'10.%';
