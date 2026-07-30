from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pymysql

DELETE_SQL = """
    DELETE FROM audit_api_call_log
    WHERE called_at < %s
    ORDER BY called_at ASC, id ASC
    LIMIT %s
"""


def delete_expired_rows(
    connection,
    *,
    retention_days: int,
    batch_size: int,
    now: datetime | None = None,
) -> int:
    if retention_days < 1 or batch_size < 1:
        raise ValueError("retention days and batch size must be positive")
    cutoff = ((now or datetime.now(UTC)) - timedelta(days=retention_days)).replace(tzinfo=None)
    deleted = 0
    while True:
        with connection.cursor() as cursor:
            cursor.execute(DELETE_SQL, (cutoff, batch_size))
            count = cursor.rowcount
        connection.commit()
        deleted += count
        if count < batch_size:
            return deleted


def main() -> int:
    required = {
        name: os.getenv(name)
        for name in ("AUDIT_DB_HOST", "AUDIT_DB_USER", "AUDIT_DB_PASSWORD", "AUDIT_DB_NAME")
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(f"required audit DB settings are missing: {', '.join(missing)}")
    connection = pymysql.connect(
        host=required["AUDIT_DB_HOST"],
        port=int(os.getenv("AUDIT_DB_PORT", "3306")),
        user=required["AUDIT_DB_USER"],
        password=required["AUDIT_DB_PASSWORD"],
        database=required["AUDIT_DB_NAME"],
        charset="utf8mb4",
        autocommit=False,
    )
    try:
        deleted = delete_expired_rows(
            connection,
            retention_days=int(os.getenv("AUDIT_RETENTION_DAYS", "90")),
            batch_size=int(os.getenv("AUDIT_DELETE_BATCH_SIZE", "1000")),
        )
    finally:
        connection.close()
    print(f"audit retention completed deleted_rows={deleted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
