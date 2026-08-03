CREATE OR REPLACE SQL SECURITY DEFINER VIEW
    `jw_market_audit_stage`.`dashboard_chat_usage_test2_v` AS
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
) s ON s.`uid` = o.`session_id`
UNION ALL
SELECT
    NULL AS `conversation_log_id`,
    r.`portal_user_id`,
    r.`session_uid` AS `conversation_id`,
    r.`turn_index`,
    'complete' AS `contract_status`,
    'rnd_trace' AS `quality_label`,
    NULL AS `elapsed_ms`,
    r.`service_id`,
    r.`trace_id`,
    NULL AS `request_id`,
    'false' AS `token_usage_available`,
    0 AS `input_tokens`,
    0 AS `output_tokens`,
    0 AS `total_tokens`,
    r.`created_at`
FROM `jw_mart`.`rnd_trace_conversation_log` r
WHERE r.`source_system` = 'genos_monitoring' AND r.`service_id` = 61;

GRANT SELECT ON `jw_market_audit_stage`.`dashboard_chat_usage_test2_v`
    TO 'jw_market_audit_reader_stage'@'10.%';
