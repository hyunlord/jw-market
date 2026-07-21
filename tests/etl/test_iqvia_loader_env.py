"""Unit tests for iqvia_loader .env tolerance (R-1 load_iqvia/s2_catalog fix).

connect() must NOT hard-require pipeline/docker/.env: in k8s/rehearsal the file
is absent and MARIADB_* come from injected env vars. Mirrors general_config.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.etl.io import iqvia_loader


def test_load_env_missing_file_falls_back_to_environ(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in ("MARIADB_USER", "MARIADB_PASSWORD", "MARIADB_HOST", "HOST_PORT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MARIADB_USER", "root")
    monkeypatch.setenv("MARIADB_PASSWORD", "envpw")
    monkeypatch.setenv("HOST_PORT", "3306")
    monkeypatch.setenv("UNRELATED", "x")

    env = iqvia_loader.load_env(tmp_path / "does_not_exist.env")

    # Only MARIADB_*/HOST_PORT surface; no raise on the missing file.
    assert env["MARIADB_USER"] == "root"
    assert env["MARIADB_PASSWORD"] == "envpw"
    assert env["HOST_PORT"] == "3306"
    assert "UNRELATED" not in env


def test_load_env_existing_file_parsed_with_shell_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\nMARIADB_USER=fileuser\nMARIADB_PASSWORD=filepw\nMARIADB_HOST=filehost\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("MARIADB_USER", raising=False)
    monkeypatch.setenv("MARIADB_PASSWORD", "shellpw")  # shell wins over file

    env = iqvia_loader.load_env(env_file)

    assert env["MARIADB_USER"] == "fileuser"       # file value kept (no shell override)
    assert env["MARIADB_PASSWORD"] == "shellpw"    # shell env overrides file
    assert env["MARIADB_HOST"] == "filehost"


def test_connect_does_not_raise_when_env_file_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Force the .env lookup to a path that does not exist.
    monkeypatch.setattr(iqvia_loader, "first_existing", lambda *paths: tmp_path / "absent.env")
    monkeypatch.setenv("MARIADB_USER", "root")
    monkeypatch.setenv("MARIADB_ROOT_PASSWORD", "rootpw")
    monkeypatch.setenv("MARIADB_HOST", "db.example")

    captured: dict[str, object] = {}

    def fake_connect(**kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return "CONN"

    monkeypatch.setattr(iqvia_loader.pymysql, "connect", fake_connect)

    conn = iqvia_loader.connect("jw_mart_rehearsal_r1_test")

    assert conn == "CONN"                                  # no FileNotFoundError
    assert captured["user"] == "root"
    assert captured["password"] == "rootpw"                # root uses ROOT_PASSWORD
    assert captured["host"] == "db.example"
    assert captured["database"] == "jw_mart_rehearsal_r1_test"


def test_connect_still_raises_when_no_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(iqvia_loader, "first_existing", lambda *paths: tmp_path / "absent.env")
    for key in ("MARIADB_PASSWORD", "MARIADB_ROOT_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MARIADB_USER", "jwapp")

    with pytest.raises(RuntimeError, match="password is not configured"):
        iqvia_loader.connect()
