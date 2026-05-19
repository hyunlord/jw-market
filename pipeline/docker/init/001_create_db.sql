-- Phase 16-B local MariaDB Galera bootstrap.
-- Real Layer 1/2/3/4 mart schema migrations are applied in later steps.

SET GLOBAL time_zone = '+09:00';

CREATE DATABASE IF NOT EXISTS jw_mart
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

ALTER DATABASE jw_mart
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

GRANT ALL PRIVILEGES ON jw_mart.* TO 'jwapp'@'%';
FLUSH PRIVILEGES;

USE jw_mart;

CREATE TABLE IF NOT EXISTS _jsoncheck (
  id INT PRIMARY KEY,
  payload JSON NOT NULL,
  name VARCHAR(255)
    AS (JSON_UNQUOTE(JSON_EXTRACT(payload, '$.name'))) VIRTUAL,
  INDEX idx_name (name)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci;
