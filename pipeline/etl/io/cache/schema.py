from __future__ import annotations

from pipeline.etl.io.cache.db import connect, database_name

CACHE_TABLES = (
    "cache_brands",
    "cache_market_status",
    "cache_cause",
    "cache_deep_analysis",
    "cache_deep_analysis_general",
)


def create_cache_tables(target_db: str) -> None:
    db = database_name(target_db)
    with connect(db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS cache_brands (
                query_key VARCHAR(255) PRIMARY KEY,
                response_json LONGTEXT NOT NULL CHECK (JSON_VALID(response_json)),
                payload_size INT NOT NULL,
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
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    ON UPDATE CURRENT_TIMESTAMP
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_uca1400_ai_ci
            """
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
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    ON UPDATE CURRENT_TIMESTAMP,
                PRIMARY KEY (brand_key, atc4_code),
                INDEX idx_cache_deep_general_brand (brand),
                INDEX idx_cache_deep_general_atc4 (atc4_code),
                INDEX idx_cache_deep_general_market (market_id)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_uca1400_ai_ci
            """
        )
