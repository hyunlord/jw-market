from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient
import pytest


def _load_local_db_env() -> None:
    env_path = Path("pipeline/docker/.env")
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key == "MARIADB_ROOT_PASSWORD":
            os.environ.setdefault("DB_PASSWORD", value.strip().strip('"').strip("'"))
        elif key == "MARIADB_DATABASE":
            os.environ.setdefault("DB_NAME", value.strip().strip('"').strip("'"))
        elif key == "HOST_PORT":
            os.environ.setdefault("DB_PORT", value.strip().strip('"').strip("'"))
    os.environ.setdefault("DB_HOST", "127.0.0.1")
    os.environ.setdefault("DB_USER", "root")
    os.environ.setdefault("APP_VERSION", "0.10.0")


_load_local_db_env()

from pipeline.scripts.api.main import app  # noqa: E402


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)
