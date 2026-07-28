"""Writer lock and crash-recovery journal for CSD table publication."""
from __future__ import annotations

from datetime import datetime, timezone

from pipeline.scripts.ingest_hook.csd_publication_provenance import (
    Connection,
    Cursor,
    bounded_id,
    delete_if_present,
    quote_id,
    table_exists,
)


JOURNAL_TABLE = "ingest_csd_activation_journal"


class ActivationJournal:
    def __init__(
        self,
        conn: Connection,
        *,
        category: str,
        live_raw: str,
        live_stage: str,
        raw_table: str,
        stage_table: str,
        backup_raw: str,
        backup_stage: str,
    ) -> None:
        self.conn = conn
        self.category = category
        self.live_raw = live_raw
        self.live_stage = live_stage
        self.raw_table = raw_table
        self.stage_table = stage_table
        self.backup_raw = backup_raw
        self.backup_stage = backup_stage
        self.lock_name = f"jw_ingest_csd:{category}"

    def acquire(self) -> None:
        cursor = self.conn.cursor()
        try:
            cursor.execute("SELECT GET_LOCK(%s, %s)", (self.lock_name, 0))
            row = cursor.fetchone()
            if row is None or int(row[0]) != 1:
                raise RuntimeError(
                    f"CSD publication writer lock is already held: {self.lock_name}"
                )
        finally:
            cursor.close()

    def release(self) -> None:
        cursor = self.conn.cursor()
        try:
            cursor.execute("SELECT RELEASE_LOCK(%s)", (self.lock_name,))
            row = cursor.fetchone()
            if row is None or int(row[0]) != 1:
                raise RuntimeError(
                    f"CSD publication writer lock release failed: {self.lock_name}"
                )
        finally:
            cursor.close()

    def _ensure_table(self, cursor: Cursor) -> None:
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {quote_id(self.live_stage)}.{quote_id(JOURNAL_TABLE)} (
              category VARCHAR(32) PRIMARY KEY,
              run_id VARCHAR(64) NOT NULL,
              phase VARCHAR(32) NOT NULL,
              backup_raw VARCHAR(128) NOT NULL,
              backup_stage VARCHAR(128) NOT NULL,
              updated_at_utc VARCHAR(40) NOT NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )

    def recover(self) -> None:
        cursor = self.conn.cursor()
        try:
            self._ensure_table(cursor)
            cursor.execute(
                f"SELECT run_id, phase, backup_raw, backup_stage FROM "
                f"{quote_id(self.live_stage)}.{quote_id(JOURNAL_TABLE)} "
                "WHERE category = %s",
                (self.category,),
            )
            row = cursor.fetchone()
            if row is None or str(row[1]) in {"complete", "rolled_back", "recovered"}:
                self.conn.commit()
                return
            previous_run = str(row[0])
            phase = str(row[1])
            backup_raw = str(row[2])
            backup_stage = str(row[3])
            raw_exists = table_exists(cursor, self.live_raw, backup_raw)
            stage_exists = table_exists(cursor, self.live_stage, backup_stage)
            if raw_exists != stage_exists:
                raise RuntimeError(
                    "incomplete CSD activation has a partial backup table set"
                )
            if raw_exists:
                self._restore(cursor, previous_run, backup_raw, backup_stage)
                delete_if_present(
                    cursor,
                    live_stage=self.live_stage,
                    category=self.category,
                    run_id=previous_run,
                )
            elif phase != "armed":
                raise RuntimeError(
                    "incomplete CSD activation lost both backup tables after "
                    f"publication phase={phase}"
                )
            self._mark(cursor, run_id=previous_run, phase="recovered")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        finally:
            cursor.close()

    def _restore(
        self,
        cursor: Cursor,
        previous_run: str,
        backup_raw: str,
        backup_stage: str,
    ) -> None:
        q = quote_id
        failed_raw = bounded_id(self.raw_table, "recovery", previous_run)
        failed_stage = bounded_id(self.stage_table, "recovery", previous_run)
        cursor.execute(
            "RENAME TABLE "
            f"{q(self.live_raw)}.{q(self.raw_table)} TO "
            f"{q(self.live_raw)}.{q(failed_raw)}, "
            f"{q(self.live_raw)}.{q(backup_raw)} TO "
            f"{q(self.live_raw)}.{q(self.raw_table)}, "
            f"{q(self.live_stage)}.{q(self.stage_table)} TO "
            f"{q(self.live_stage)}.{q(failed_stage)}, "
            f"{q(self.live_stage)}.{q(backup_stage)} TO "
            f"{q(self.live_stage)}.{q(self.stage_table)}"
        )

    def arm(self, run_id: str) -> None:
        cursor = self.conn.cursor()
        try:
            self._ensure_table(cursor)
            cursor.execute(
                f"INSERT INTO {quote_id(self.live_stage)}.{quote_id(JOURNAL_TABLE)} "
                "(category, run_id, phase, backup_raw, backup_stage, updated_at_utc) "
                "VALUES (%s, %s, 'armed', %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE run_id=VALUES(run_id), phase=VALUES(phase), "
                "backup_raw=VALUES(backup_raw), backup_stage=VALUES(backup_stage), "
                "updated_at_utc=VALUES(updated_at_utc)",
                (
                    self.category,
                    run_id,
                    self.backup_raw,
                    self.backup_stage,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        finally:
            cursor.close()

    def mark(self, *, run_id: str, phase: str) -> None:
        cursor = self.conn.cursor()
        try:
            self._mark(cursor, run_id=run_id, phase=phase)
            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"CSD activation journal phase was not recorded: {phase}"
                )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        finally:
            cursor.close()

    def _mark(self, cursor: Cursor, *, run_id: str, phase: str) -> None:
        cursor.execute(
            f"UPDATE {quote_id(self.live_stage)}.{quote_id(JOURNAL_TABLE)} "
            "SET phase = %s, updated_at_utc = %s "
            "WHERE category = %s AND run_id = %s",
            (
                phase,
                datetime.now(timezone.utc).isoformat(),
                self.category,
                run_id,
            ),
        )
