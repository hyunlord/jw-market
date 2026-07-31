from __future__ import annotations

from typing import Any

from pipeline.scripts.deploy.analysis_cache_db import table_row_count
from pipeline.scripts.deploy.mart_load_verify import quote_id, table_digest, table_exists


class MySQLMart:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def exists(self, db_name: str, table_name: str) -> bool:
        return table_exists(self._conn, db_name, table_name)

    def count(self, db_name: str, table_name: str) -> int:
        return table_row_count(self._conn, db_name, table_name)

    def digest(self, db_name: str, table_name: str) -> str:
        digest = table_digest(self._conn, db_name, table_name)
        return f"{digest.row_count}:{digest.crc_sum}:{digest.crc_xor}"

    def rename(self, db_name: str, moves: tuple[tuple[str, str], ...]) -> None:
        if not moves:
            raise ValueError("atomic rollback requires at least one table move")
        rendered = ", ".join(
            f"{quote_id(db_name)}.{quote_id(source)} TO {quote_id(db_name)}.{quote_id(target)}"
            for source, target in moves
        )
        with self._conn.cursor() as cursor:
            cursor.execute(f"RENAME TABLE {rendered}")

    def invalidate_dynamic_cache(
        self,
        db_name: str,
        *,
        source: str | None = None,
    ) -> None:
        with self._conn.cursor() as cursor:
            table = (
                f"{quote_id(db_name)}."
                f"{quote_id('cache_dynamic_market_response')}"
            )
            if source is None:
                cursor.execute(
                    f"UPDATE {table} "
                    "SET expires_at=UTC_TIMESTAMP(), updated_at=UTC_TIMESTAMP()"
                )
            else:
                cursor.execute(
                    f"DELETE FROM {table} "
                    "WHERE JSON_UNQUOTE("
                    "JSON_EXTRACT(request_json, '$.source'))=%s",
                    (source,),
                )
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def drop_generation(self, db_name: str) -> None:
        with self._conn.cursor() as cursor:
            cursor.execute(f"DROP DATABASE {quote_id(db_name)}")

    def drop_backup_tables(self, db_name: str, table_names: tuple[str, ...]) -> None:
        if not table_names:
            return
        rendered = ", ".join(f"{quote_id(db_name)}.{quote_id(name)}" for name in table_names)
        with self._conn.cursor() as cursor:
            cursor.execute(f"DROP TABLE {rendered}")

    def drop_tables(self, db_name: str, table_names: tuple[str, ...]) -> None:
        self.drop_backup_tables(db_name, table_names)
