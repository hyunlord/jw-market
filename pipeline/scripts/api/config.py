from __future__ import annotations

import os
from dataclasses import dataclass

import pymysql


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


@dataclass(frozen=True)
class APIConfig:
    db_host: str
    db_port: int
    db_user: str
    db_password: str
    db_name: str
    app_version: str
    external_path_prefix: str
    log_level: str
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    cache_ttl_seconds: int = 86400


ApiSettings = APIConfig


def load_config() -> APIConfig:
    """Load API settings from env vars, with local-dev fallbacks."""
    return APIConfig(
        db_host=os.getenv("DB_HOST", "127.0.0.1"),
        db_port=_env_int("DB_PORT", 3308),
        db_user=os.getenv("DB_USER", "root"),
        db_password=os.getenv("DB_PASSWORD", ""),
        db_name=os.getenv("DB_NAME", "jw_mart"),
        app_version=os.getenv("APP_VERSION", "v0.1.0"),
        external_path_prefix=os.getenv("EXTERNAL_PATH_PREFIX", ""),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        api_host=os.getenv("API_HOST", "0.0.0.0"),
        api_port=_env_int("API_PORT", 8000),
    )


config = load_config()


DB_HOST = config.db_host
DB_PORT = config.db_port
DB_USER = config.db_user
DB_PASSWORD = config.db_password
DB_NAME = config.db_name


def get_settings() -> APIConfig:
    return config


def get_db_connection() -> pymysql.connections.Connection:
    return pymysql.connect(
        host=config.db_host,
        port=config.db_port,
        user=config.db_user,
        password=config.db_password,
        database=config.db_name,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )
