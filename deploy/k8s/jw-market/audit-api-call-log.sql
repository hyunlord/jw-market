CREATE DATABASE IF NOT EXISTS `jw_market_audit_dev`
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `jw_market_audit_dev`.`audit_api_call_log` (
    `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `actor_uid` VARCHAR(128) NULL,
    `actor_type` ENUM('user', 'service', 'unknown', 'system') NOT NULL,
    `called_at` DATETIME(6) NOT NULL,
    `endpoint` VARCHAR(255) NOT NULL,
    `request_params` JSON NOT NULL,
    `http_status` INT NOT NULL,
    `jti` VARCHAR(64) NULL,
    PRIMARY KEY (`id`),
    KEY `idx_audit_called_at` (`called_at`),
    KEY `idx_audit_actor_called_at` (`actor_uid`, `called_at`)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS `jw_market_audit_dev`.`report_download_event` (
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
