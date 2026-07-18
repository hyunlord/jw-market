from __future__ import annotations

from pipeline.etl.io.cache.db import connect, database_name

CACHE_TABLES = (
    "cache_brands",
    "cache_market_status",
    "cache_cause",
    "cache_deep_analysis",
    "cache_deep_analysis_general",
    "cache_market_forecast_general",
    "cache_brand_elements",
)


def create_cache_tables(target_db: str) -> None:
    db = database_name(target_db)
    with connect(db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS cache_dynamic_market_response (
                cache_key CHAR(64) NOT NULL,
                namespace VARCHAR(32) NOT NULL DEFAULT 'dynamic',
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
                failure_reason VARCHAR(255) NULL,
                attempt_count INT UNSIGNED NOT NULL DEFAULT 0,
                last_error TEXT NULL,
                last_attempt_at DATETIME NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                PRIMARY KEY (cache_key),
                KEY idx_dynamic_response_expiry (state, expires_at),
                KEY idx_dynamic_response_lease (state, lease_expires_at),
                KEY idx_dynamic_response_eviction (state, hit_count, last_hit_at, updated_at),
                KEY idx_dynamic_response_namespace_eviction (namespace, state, hit_count, last_hit_at, updated_at)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_uca1400_ai_ci
            """
        )
        for column in (
            "failure_reason VARCHAR(255) NULL",
            "attempt_count INT UNSIGNED NOT NULL DEFAULT 0",
            "last_error TEXT NULL",
            "last_attempt_at DATETIME NULL",
        ):
            cur.execute(f"ALTER TABLE cache_dynamic_market_response ADD COLUMN IF NOT EXISTS {column}")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS cache_brands (
                query_key VARCHAR(255) PRIMARY KEY,
                response_json LONGTEXT NOT NULL CHECK (JSON_VALID(response_json)),
                payload_size INT NOT NULL,
                build_sha VARCHAR(64) NULL,
                input_manifest_json LONGTEXT NULL,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    ON UPDATE CURRENT_TIMESTAMP
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_uca1400_ai_ci
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS cache_market_status (
                query_key VARCHAR(255) PRIMARY KEY,
                response_json LONGTEXT NOT NULL CHECK (JSON_VALID(response_json)),
                payload_size INT NOT NULL,
                build_sha VARCHAR(64) NULL,
                input_manifest_json LONGTEXT NULL,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    ON UPDATE CURRENT_TIMESTAMP
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_uca1400_ai_ci
            """
        )
        for table in ("cache_brands", "cache_market_status"):
            cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS build_sha VARCHAR(64) NULL")
            cur.execute(
                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS input_manifest_json LONGTEXT NULL"
            )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS cache_cause (
                brand VARCHAR(255) NOT NULL,
                view_type VARCHAR(30) NOT NULL,
                source VARCHAR(10) NOT NULL,
                measure VARCHAR(20) NOT NULL,
                market_id VARCHAR(20) NOT NULL,
                response_json LONGTEXT NOT NULL CHECK (JSON_VALID(response_json)),
                payload_size INT NOT NULL,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    ON UPDATE CURRENT_TIMESTAMP,
                PRIMARY KEY (brand, view_type, source, measure, market_id),
                INDEX idx_cache_cause_brand (brand),
                INDEX idx_cache_cause_market (market_id)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_uca1400_ai_ci
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS cache_deep_analysis (
                brand VARCHAR(255) PRIMARY KEY,
                market_id VARCHAR(20) NOT NULL,
                response_json LONGTEXT NOT NULL CHECK (JSON_VALID(response_json)),
                payload_size INT NOT NULL,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    ON UPDATE CURRENT_TIMESTAMP,
                INDEX idx_cache_deep_market (market_id)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_uca1400_ai_ci
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS cache_deep_analysis_general (
                brand_key VARCHAR(255) NOT NULL,
                brand VARCHAR(255) NOT NULL,
                atc4_code VARCHAR(16) NOT NULL,
                market_id VARCHAR(32) NOT NULL,
                response_json LONGTEXT NOT NULL CHECK (JSON_VALID(response_json)),
                payload_size INT NOT NULL,
                brand_factors LONGTEXT NULL CHECK (brand_factors IS NULL OR JSON_VALID(brand_factors)),
                source_computed_at TIMESTAMP NULL,
                expires_at TIMESTAMP NULL,
                is_stale TINYINT(1) NOT NULL DEFAULT 0,
                stale_reason VARCHAR(255) NULL,
                stale_marked_at TIMESTAMP NULL,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    ON UPDATE CURRENT_TIMESTAMP,
                PRIMARY KEY (brand_key, atc4_code),
                INDEX idx_cache_deep_general_brand (brand),
                INDEX idx_cache_deep_general_atc4 (atc4_code),
                INDEX idx_cache_deep_general_market (market_id),
                INDEX idx_cache_deep_general_expires (expires_at),
                INDEX idx_cache_deep_general_stale (is_stale, stale_marked_at)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_uca1400_ai_ci
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS cache_market_forecast_general (
                atc4_code VARCHAR(16) NOT NULL,
                source VARCHAR(32) NOT NULL,
                measure VARCHAR(32) NOT NULL,
                market_forecast_json LONGTEXT NOT NULL CHECK (JSON_VALID(market_forecast_json)),
                payload_size INT NOT NULL,
                source_row_count INT NOT NULL,
                source_computed_at TIMESTAMP NULL,
                expires_at TIMESTAMP NULL,
                is_stale TINYINT(1) NOT NULL DEFAULT 0,
                stale_reason VARCHAR(255) NULL,
                stale_marked_at TIMESTAMP NULL,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    ON UPDATE CURRENT_TIMESTAMP,
                PRIMARY KEY (atc4_code, source, measure),
                INDEX idx_market_forecast_expires (expires_at),
                INDEX idx_market_forecast_stale (is_stale, stale_marked_at)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_uca1400_ai_ci
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS cache_brand_elements (
                brand_key VARCHAR(255) NOT NULL,
                brand_name VARCHAR(255) NOT NULL,
                brand_name_compact VARCHAR(255) NOT NULL,
                factors_json LONGTEXT NOT NULL CHECK (JSON_VALID(factors_json)),
                strength_json LONGTEXT NOT NULL CHECK (JSON_VALID(strength_json)),
                strength_generated_at DATETIME NULL,
                strength_workflow_rev VARCHAR(64) NULL,
                source_computed_at TIMESTAMP NULL,
                expires_at TIMESTAMP NULL,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                PRIMARY KEY (brand_key),
                KEY idx_cache_brand_elements_compact (brand_name_compact),
                KEY idx_cache_brand_elements_updated_at (updated_at),
                KEY idx_cache_brand_elements_expires (expires_at)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_uca1400_ai_ci
            """
        )
