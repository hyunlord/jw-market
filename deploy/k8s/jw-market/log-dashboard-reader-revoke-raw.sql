-- One-time privilege migration. Run only after the sanitized dashboard views exist.
REVOKE SELECT ON `jw_market_audit_stage`.`audit_api_call_log`
    FROM 'jw_market_audit_reader_stage'@'10.%';
REVOKE SELECT ON `llmops`.`user_tb`
    FROM 'jw_market_audit_reader_stage'@'10.%';
