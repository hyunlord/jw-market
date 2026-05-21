from __future__ import annotations

import importlib

import pytest


ENV_KEYS = [
    "DB_HOST",
    "DB_PORT",
    "DB_USER",
    "DB_PASSWORD",
    "DB_NAME",
    "APP_VERSION",
    "EXTERNAL_PATH_PREFIX",
    "LOG_LEVEL",
    "API_HOST",
    "API_PORT",
]


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _reload_config():
    from pipeline.scripts.api import config as config_mod

    return importlib.reload(config_mod)


def test_config_default_local_dev(clean_env: None) -> None:
    config_mod = _reload_config()

    assert config_mod.config.db_host == "127.0.0.1"
    assert config_mod.config.db_port == 3308
    assert config_mod.config.db_user == "root"
    assert config_mod.config.db_password == ""
    assert config_mod.config.db_name == "jw_mart"
    assert config_mod.config.app_version == "v0.1.0"
    assert config_mod.config.external_path_prefix == ""
    assert config_mod.config.log_level == "INFO"


def test_config_production_env(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_HOST", "llmops-mariadb-service.llmops.svc.cluster.local")
    monkeypatch.setenv("DB_PORT", "3306")
    monkeypatch.setenv("DB_USER", "llmops")
    monkeypatch.setenv("DB_PASSWORD", "secret123")
    monkeypatch.setenv("DB_NAME", "jw_mart")
    monkeypatch.setenv("APP_VERSION", "v0.1.0")
    monkeypatch.setenv("EXTERNAL_PATH_PREFIX", "/jw-market-backend-api")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("API_HOST", "127.0.0.1")
    monkeypatch.setenv("API_PORT", "9000")

    config_mod = _reload_config()

    assert config_mod.config.db_host == "llmops-mariadb-service.llmops.svc.cluster.local"
    assert config_mod.config.db_port == 3306
    assert config_mod.config.db_user == "llmops"
    assert config_mod.config.db_password == "secret123"
    assert config_mod.config.db_name == "jw_mart"
    assert config_mod.config.app_version == "v0.1.0"
    assert config_mod.config.external_path_prefix == "/jw-market-backend-api"
    assert config_mod.config.log_level == "DEBUG"
    assert config_mod.config.api_host == "127.0.0.1"
    assert config_mod.config.api_port == 9000


def test_config_backwards_compatible_module_names(clean_env: None) -> None:
    config_mod = _reload_config()

    assert config_mod.DB_HOST == config_mod.config.db_host
    assert config_mod.DB_PORT == config_mod.config.db_port
    assert config_mod.DB_USER == config_mod.config.db_user
    assert config_mod.DB_PASSWORD == config_mod.config.db_password
    assert config_mod.DB_NAME == config_mod.config.db_name
    assert config_mod.get_settings() == config_mod.config


def test_get_db_connection_uses_loaded_config(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    monkeypatch.setenv("DB_HOST", "test-host")
    monkeypatch.setenv("DB_PORT", "1234")
    monkeypatch.setenv("DB_USER", "test-user")
    monkeypatch.setenv("DB_PASSWORD", "test-password")
    monkeypatch.setenv("DB_NAME", "test-db")

    config_mod = _reload_config()

    def fake_connect(**kwargs):
        calls.append(kwargs)
        return object()

    monkeypatch.setattr("pymysql.connect", fake_connect)

    assert config_mod.get_db_connection() is not None
    assert calls == [
        {
            "host": "test-host",
            "port": 1234,
            "user": "test-user",
            "password": "test-password",
            "database": "test-db",
            "charset": "utf8mb4",
            "cursorclass": config_mod.pymysql.cursors.DictCursor,
            "autocommit": True,
        }
    ]


def test_main_accepts_external_path_prefixed_health(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXTERNAL_PATH_PREFIX", "/jw-market-backend-api")

    from pipeline.scripts.api import config as config_mod
    from pipeline.scripts.api import main as main_mod

    importlib.reload(config_mod)
    main_mod = importlib.reload(main_mod)

    from fastapi.testclient import TestClient

    client = TestClient(main_mod.app)
    assert client.get("/jw-market-backend-api/api/health").status_code == 200
