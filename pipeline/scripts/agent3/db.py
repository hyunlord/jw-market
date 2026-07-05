from __future__ import annotations

from dataclasses import dataclass
import os

import pymysql


@dataclass(frozen=True, slots=True)
class DbConfig:
    host: str
    port: int
    user: str
    password: str
    database: str

    @classmethod
    def from_env(cls) -> "DbConfig":
        return cls(
            host=os.environ.get("AGENT3_DB_HOST") or os.environ.get("DB_HOST", "127.0.0.1"),
            port=int(os.environ.get("AGENT3_DB_PORT") or os.environ.get("DB_PORT", "3306")),
            user=os.environ.get("AGENT3_DB_USER") or os.environ.get("DB_USER", "root"),
            password=os.environ.get("AGENT3_DB_PASSWORD") or os.environ.get("DB_PASSWORD", ""),
            database=os.environ.get("AGENT3_DB_NAME") or os.environ.get("DB_NAME", "jw_mart"),
        )


def connect(config: DbConfig | None = None) -> pymysql.connections.Connection:
    cfg = config or DbConfig.from_env()
    return pymysql.connect(
        host=cfg.host,
        port=cfg.port,
        user=cfg.user,
        password=cfg.password,
        database=cfg.database,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )

