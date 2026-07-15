from __future__ import annotations

import json
import math
import os
from typing import Any, Final

import pymysql

_FLOAT_TYPE: Final[type[float]] = float


def mariadb_connect() -> pymysql.connections.Connection:
    return pymysql.connect(
        host=os.environ.get("MARIADB_HOST", "127.0.0.1"),
        port=int(os.environ.get("MARIADB_PORT", "3308")),
        user=os.environ.get("MARIADB_USER", "root"),
        password=os.environ.get("MARIADB_PASSWORD") or os.environ.get("MYSQL_PWD"),
        database=os.environ.get("MARIADB_DATABASE", "jw_mart"),
        charset="utf8mb4",
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


def safe_float(value: Any) -> float | None:
    if isinstance(value, _FLOAT_TYPE):
        return value if math.isfinite(value) else None
    try:
        if value is None:
            return None
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def json_ready(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): json_ready(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_ready(v) for v in value]
    if hasattr(value, "item"):
        return json_ready(value.item())
    return value


def dumps(value: Any) -> str:
    return json.dumps(json_ready(value), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
