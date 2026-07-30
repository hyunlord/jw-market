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
