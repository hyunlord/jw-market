from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pipeline.scripts.ops_utils import find_project_root, first_existing


PROJECT_ROOT = find_project_root(Path(__file__).resolve())
ENV_PATH = first_existing(PROJECT_ROOT / "pipeline" / "docker" / ".env", PROJECT_ROOT / "docker" / ".env")


def load_env(path: Path = ENV_PATH) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


@dataclass(frozen=True)
class ApiSettings:
    db_host: str
    db_port: int
    db_user: str
    db_password: str
    db_name: str
    cache_ttl_seconds: int = 86400


def get_settings() -> ApiSettings:
    env = load_env()
    password = env.get("MARIADB_PASSWORD")
    if not password:
        raise RuntimeError(f"MARIADB_PASSWORD is missing in {ENV_PATH}")
    return ApiSettings(
        db_host="127.0.0.1",
        db_port=int(env.get("HOST_PORT", "3307")),
        db_user=env.get("MARIADB_USER", "jwapp"),
        db_password=password,
        db_name=env.get("MARIADB_DATABASE", "jw_mart"),
    )
