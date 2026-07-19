from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
from typing import Any

from pipeline.scripts.rollback.models import (
    REQUIRED_COMPONENTS,
    PromotionGeneration,
    RollbackEvent,
    TableBackup,
)


_GENERATION_DDL = """
CREATE TABLE IF NOT EXISTS promotion_generation (
  promotion_run_id VARCHAR(64) PRIMARY KEY,
  epoch VARCHAR(32) NOT NULL,
  ingest_run_id VARCHAR(64) NOT NULL,
  serving_db VARCHAR(128) NOT NULL,
  generation_db VARCHAR(128) NOT NULL,
  status VARCHAR(16) NOT NULL,
  promoted_at VARCHAR(32) NOT NULL
)
"""
_COMPONENT_DDL = """
CREATE TABLE IF NOT EXISTS promotion_component (
  promotion_run_id VARCHAR(64) NOT NULL,
  component VARCHAR(32) NOT NULL,
  tables_json TEXT NOT NULL,
  PRIMARY KEY (promotion_run_id, component)
)
"""
_ROLLBACK_DDL = """
CREATE TABLE IF NOT EXISTS promotion_rollback_event (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  promotion_run_id VARCHAR(64) NOT NULL,
  actor VARCHAR(128) NOT NULL,
  reason TEXT NOT NULL,
  rolled_back_at VARCHAR(32) NOT NULL
)
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class PromotionLedger:
    def __init__(self, conn: Any, *, dialect: str) -> None:
        if dialect not in {"sqlite", "mysql"}:
            raise ValueError(f"unsupported ledger dialect: {dialect}")
        self._conn = conn
        self._dialect = dialect
        self._mark = "?" if dialect == "sqlite" else "%s"

    def ensure_tables(self) -> None:
        rollback_ddl = _ROLLBACK_DDL
        if self._dialect == "mysql":
            rollback_ddl = rollback_ddl.replace(
                "id INTEGER PRIMARY KEY AUTOINCREMENT", "id BIGINT AUTO_INCREMENT PRIMARY KEY"
            )
        cursor = self._conn.cursor()
        for statement in (_GENERATION_DDL, _COMPONENT_DDL, rollback_ddl):
            cursor.execute(statement)
        self._conn.commit()

    def _execute(self, sql: str, params: tuple[object, ...] = ()) -> Any:
        cursor = self._conn.cursor()
        rendered = sql if self._dialect == "sqlite" else sql.replace("?", self._mark)
        cursor.execute(rendered, params)
        return cursor

    def record_generation(
        self,
        promotion_run_id: str,
        epoch: str,
        ingest_run_id: str,
        serving_db: str,
        generation_db: str,
        *,
        status: str = "good",
    ) -> None:
        existing = self._execute(
            "SELECT epoch, ingest_run_id, serving_db, generation_db FROM promotion_generation "
            "WHERE promotion_run_id=?",
            (promotion_run_id,),
        ).fetchone()
        identity = (epoch, ingest_run_id, serving_db, generation_db)
        if existing is not None:
            values = tuple(existing.values()) if isinstance(existing, dict) else tuple(existing)
            if values != identity:
                raise RuntimeError(f"promotion run identity conflict: {promotion_run_id}")
            return
        self._execute(
            "INSERT INTO promotion_generation "
            "(promotion_run_id, epoch, ingest_run_id, serving_db, generation_db, status, promoted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (*((promotion_run_id,) + identity), status, _now()),
        )
        self._conn.commit()

    def record_component(
        self,
        *,
        promotion_run_id: str,
        component: str,
        epoch: str,
        ingest_run_id: str,
        target_db: str,
        generation_db: str,
        tables: tuple[TableBackup, ...],
    ) -> None:
        if not tables:
            raise ValueError("promotion component requires at least one backup table")
        self.record_generation(
            promotion_run_id,
            epoch,
            ingest_run_id,
            target_db,
            generation_db,
            status="building",
        )
        payload = json.dumps([asdict(table) for table in tables], sort_keys=True, separators=(",", ":"))
        existing = self._execute(
            "SELECT tables_json FROM promotion_component "
            "WHERE promotion_run_id=? AND component=?",
            (promotion_run_id, component),
        ).fetchone()
        if existing is not None:
            raw = next(iter(existing.values())) if isinstance(existing, dict) else existing[0]
            if raw != payload:
                raise RuntimeError(
                    f"promotion component identity conflict: {promotion_run_id}/{component}"
                )
            self._mark_good_when_complete(promotion_run_id)
            return
        self._execute(
            "INSERT INTO promotion_component (promotion_run_id, component, tables_json) VALUES (?, ?, ?)",
            (promotion_run_id, component, payload),
        )
        self._mark_good_when_complete(promotion_run_id)
        self._conn.commit()

    def _mark_good_when_complete(self, promotion_run_id: str) -> None:
        rows = self._execute(
            "SELECT component FROM promotion_component WHERE promotion_run_id=?",
            (promotion_run_id,),
        ).fetchall()
        components = {
            str(next(iter(row.values())) if isinstance(row, dict) else row[0])
            for row in rows
        }
        if components == REQUIRED_COMPONENTS:
            self._execute(
                "UPDATE promotion_generation SET status=? WHERE promotion_run_id=?",
                ("good", promotion_run_id),
            )

    def generation(self, target: str) -> PromotionGeneration | None:
        if target == "latest-good":
            cursor = self._execute(
                "SELECT promotion_run_id, epoch, ingest_run_id, serving_db, generation_db, status, promoted_at "
                "FROM promotion_generation WHERE status=? ORDER BY promoted_at DESC, promotion_run_id DESC LIMIT 1",
                ("good",),
            )
        else:
            cursor = self._execute(
                "SELECT promotion_run_id, epoch, ingest_run_id, serving_db, generation_db, status, promoted_at "
                "FROM promotion_generation WHERE promotion_run_id=?",
                (target,),
            )
        row = cursor.fetchone()
        return self._generation(row) if row is not None else None

    def generation_for_epoch(self, epoch: str) -> PromotionGeneration | None:
        row = self._execute(
            "SELECT promotion_run_id, epoch, ingest_run_id, serving_db, generation_db, status, promoted_at "
            "FROM promotion_generation WHERE epoch=? AND status=? "
            "ORDER BY promoted_at DESC, promotion_run_id DESC LIMIT 1",
            (epoch, "good"),
        ).fetchone()
        return self._generation(row) if row is not None else None

    def generations(self) -> tuple[PromotionGeneration, ...]:
        rows = self._execute(
            "SELECT promotion_run_id, epoch, ingest_run_id, serving_db, generation_db, status, promoted_at "
            "FROM promotion_generation ORDER BY promoted_at DESC, promotion_run_id DESC"
        ).fetchall()
        return tuple(self._generation(row) for row in rows)

    def components(self, promotion_run_id: str) -> dict[str, tuple[TableBackup, ...]]:
        rows = self._execute(
            "SELECT component, tables_json FROM promotion_component WHERE promotion_run_id=?",
            (promotion_run_id,),
        ).fetchall()
        result: dict[str, tuple[TableBackup, ...]] = {}
        for row in rows:
            component, raw = tuple(row.values()) if isinstance(row, dict) else tuple(row)
            result[str(component)] = tuple(TableBackup(**item) for item in json.loads(raw))
        return result

    def record_rollback(self, promotion_run_id: str, *, actor: str, reason: str) -> None:
        rolled_back_at = _now()
        self._execute(
            "INSERT INTO promotion_rollback_event (promotion_run_id, actor, reason, rolled_back_at) "
            "VALUES (?, ?, ?, ?)",
            (promotion_run_id, actor, reason, rolled_back_at),
        )
        self._execute(
            "UPDATE promotion_generation SET status=? WHERE promotion_run_id=?",
            ("rolled_back", promotion_run_id),
        )
        self._conn.commit()

    def rollback_events(self, promotion_run_id: str) -> tuple[RollbackEvent, ...]:
        rows = self._execute(
            "SELECT promotion_run_id, actor, reason, rolled_back_at FROM promotion_rollback_event "
            "WHERE promotion_run_id=? ORDER BY id",
            (promotion_run_id,),
        ).fetchall()
        return tuple(RollbackEvent(*tuple(row.values()) if isinstance(row, dict) else tuple(row)) for row in rows)

    def delete_component_for_test(self, promotion_run_id: str, component: str) -> None:
        if self._dialect != "sqlite":
            raise RuntimeError("test helper is restricted to sqlite")
        self._execute(
            "DELETE FROM promotion_component WHERE promotion_run_id=? AND component=?",
            (promotion_run_id, component),
        )
        self._conn.commit()

    @staticmethod
    def _generation(row: Any) -> PromotionGeneration:
        values = tuple(row.values()) if isinstance(row, dict) else tuple(row)
        return PromotionGeneration(*values)
