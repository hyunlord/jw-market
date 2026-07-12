"""Configuration boundary for the dormant file-SQL feature."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class FileSqlConfig:
    """Runtime limits for session-local SQLite queries."""

    enabled: bool = False
    root_dir: Path = Path("/tmp/file_sql")
    query_timeout_seconds: float = 3.0
    row_limit: int = 1_000
    insert_batch_rows: int = 100

    @classmethod
    def from_env(cls) -> FileSqlConfig:
        return cls(
            enabled=os.environ.get("FILE_SQL_ENABLED", "false").lower()
            in {"1", "true", "yes", "on"},
            root_dir=Path(os.environ.get("FILE_SQL_ROOT_DIR", "/tmp/file_sql")),
            query_timeout_seconds=float(
                os.environ.get("FILE_SQL_QUERY_TIMEOUT_SECONDS", "3")
            ),
            row_limit=int(os.environ.get("FILE_SQL_ROW_LIMIT", "1000")),
            insert_batch_rows=max(
                1, int(os.environ.get("FILE_SQL_INSERT_BATCH_ROWS", "100"))
            ),
        )
