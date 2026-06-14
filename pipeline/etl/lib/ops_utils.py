"""Small operational helpers shared by pipeline scripts."""

from __future__ import annotations

import functools
import json
import logging
import os
import time
from pathlib import Path
from typing import Callable, TypeVar

T = TypeVar("T")


def find_project_root(start: Path) -> Path:
    """Resolve the project root, allowing containers to override it."""
    env_root = os.getenv("PROJECT_ROOT")
    if env_root:
        root = Path(env_root).expanduser().resolve()
        if not root.exists():
            raise RuntimeError(f"PROJECT_ROOT does not exist: {root}")
        return root

    for candidate in [start.resolve(), *start.resolve().parents]:
        if (candidate / "catalog").is_dir() and (candidate / "data").is_dir():
            return candidate
        if (candidate / ".git").exists() and (candidate / "pipeline" / "etl").is_dir():
            return candidate
    raise RuntimeError(f"Unable to locate project root from {start}")


def first_existing(*paths: Path) -> Path:
    for path in paths:
        if path.exists():
            return path
    return paths[0]


def require_path(path: Path, label: str = "path") -> Path:
    if not path.exists():
        raise FileNotFoundError(f"Missing {label}: {path}")
    return path


class JsonFormatter(logging.Formatter):
    """Minimal JSON log formatter for Cloud Logging friendly stdout."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(name: str | None = None) -> logging.Logger:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler()
        if os.getenv("STRUCTURED_LOGS", "").lower() in {"1", "true", "yes", "json"} or os.getenv("LOG_FORMAT", "").lower() == "json":
            handler.setFormatter(JsonFormatter())
        else:
            handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        root.addHandler(handler)
    root.setLevel(level)
    return logging.getLogger(name)


def retry(
    exceptions: tuple[type[BaseException], ...] = (Exception,),
    *,
    attempts: int | None = None,
    base_delay: float | None = None,
    logger: logging.Logger | None = None,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Retry transient operations with exponential backoff."""
    max_attempts = int(os.getenv("ETL_RETRY_ATTEMPTS", str(attempts or 3)))
    delay = float(os.getenv("ETL_RETRY_BASE_SECONDS", str(base_delay or 1.0)))

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> T:
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:
                    if attempt >= max_attempts:
                        raise
                    if logger:
                        logger.warning(
                            "retrying %s after %s (attempt %s/%s)",
                            func.__name__,
                            exc,
                            attempt,
                            max_attempts,
                        )
                    time.sleep(delay * (2 ** (attempt - 1)))
            raise RuntimeError("unreachable retry state")

        return wrapper

    return decorator
