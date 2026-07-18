from __future__ import annotations

from pipeline.scripts.deploy import analysis_cache_db


def test_connect_admin_accepts_runtime_db_environment(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_connect(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    for name in (
        "MARIADB_ROOT_PASSWORD",
        "MARIADB_PASSWORD",
        "MARIADB_USER",
        "MARIADB_HOST",
        "MARIADB_PORT",
        "HOST_PORT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DB_HOST", "db.internal")
    monkeypatch.setenv("DB_PORT", "3306")
    monkeypatch.setenv("DB_USER", "runtime-user")
    monkeypatch.setenv("DB_PASSWORD", "runtime-password")
    monkeypatch.setattr(analysis_cache_db.pymysql, "connect", fake_connect)

    analysis_cache_db.connect_admin()

    assert captured["host"] == "db.internal"
    assert captured["port"] == 3306
    assert captured["user"] == "runtime-user"
    assert captured["password"] == "runtime-password"
    assert captured["autocommit"] is True
