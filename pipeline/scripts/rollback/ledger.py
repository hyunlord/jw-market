from __future__ import annotations

import json
import re
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from pipeline.scripts.rollback.models import (
    FdmActivationEvent,
    FdmRollbackPlan,
    FdmRollbackState,
    REQUIRED_COMPONENTS,
    PromotionGeneration,
    RollbackEvent,
    TableBackup,
)

_SCHEMA_RE = re.compile(r"^[A-Za-z0-9_]+$")

_GENERATION_DDL = """
CREATE TABLE IF NOT EXISTS {table} (
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
CREATE TABLE IF NOT EXISTS {table} (
  promotion_run_id VARCHAR(64) NOT NULL,
  component VARCHAR(32) NOT NULL,
  tables_json TEXT NOT NULL,
  PRIMARY KEY (promotion_run_id, component)
)
"""
_ROLLBACK_DDL = """
CREATE TABLE IF NOT EXISTS {table} (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  promotion_run_id VARCHAR(64) NOT NULL,
  actor VARCHAR(128) NOT NULL,
  reason TEXT NOT NULL,
  rolled_back_at VARCHAR(32) NOT NULL
)
"""
_FDM_ROLLBACK_DDL = """
CREATE TABLE IF NOT EXISTS {table} (
  promotion_run_id VARCHAR(64) PRIMARY KEY,
  target_db VARCHAR(128) NOT NULL,
  live_table VARCHAR(64) NOT NULL,
  backup_table VARCHAR(64) NOT NULL,
  failed_table VARCHAR(64) NOT NULL,
  expected_rows BIGINT NOT NULL,
  expected_digest VARCHAR(128) NOT NULL,
  pre_live_rows BIGINT NOT NULL,
  pre_live_digest VARCHAR(128) NOT NULL,
  actor VARCHAR(128) NOT NULL,
  reason TEXT NOT NULL,
  state VARCHAR(32) NOT NULL,
  updated_at VARCHAR(32) NOT NULL,
  last_error TEXT NULL
)
"""
_FDM_ACTIVATION_DDL = """
CREATE TABLE IF NOT EXISTS {table} (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  promotion_run_id VARCHAR(64) NOT NULL,
  target_db VARCHAR(128) NOT NULL,
  live_table VARCHAR(64) NOT NULL,
  stage_table VARCHAR(64) NOT NULL,
  backup_table VARCHAR(64) NOT NULL,
  source VARCHAR(32) NOT NULL,
  event VARCHAR(32) NOT NULL,
  batch_index BIGINT NULL,
  rows_affected BIGINT NOT NULL,
  pre_live_rows BIGINT NULL,
  pre_live_digest VARCHAR(128) NULL,
  recorded_at VARCHAR(32) NOT NULL,
  last_error TEXT NULL
)
"""

_FDM_ACTIVATION_TERMINAL_EVENTS = frozenset({"completed", "compensated"})


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class PromotionLedger:
    def __init__(self, conn: Any, *, dialect: str, schema_db: str | None = None) -> None:
        if dialect not in {"sqlite", "mysql"}:
            raise ValueError(f"unsupported ledger dialect: {dialect}")
        if dialect == "mysql" and (
            not schema_db or _SCHEMA_RE.fullmatch(schema_db) is None
        ):
            raise ValueError(
                "mysql promotion ledger schema_db must contain only letters, numbers, and underscores"
            )
        self._conn = conn
        self._dialect = dialect
        self._schema_db = schema_db
        self._mark = "?" if dialect == "sqlite" else "%s"

    def ensure_tables(self) -> None:
        generation_ddl = _GENERATION_DDL.format(
            table=self._table("promotion_generation")
        )
        component_ddl = _COMPONENT_DDL.format(
            table=self._table("promotion_component")
        )
        rollback_ddl = _ROLLBACK_DDL.format(
            table=self._table("promotion_rollback_event")
        )
        fdm_rollback_ddl = _FDM_ROLLBACK_DDL.format(
            table=self._table("promotion_fdm_rollback_state")
        )
        fdm_activation_ddl = _FDM_ACTIVATION_DDL.format(
            table=self._table("promotion_fdm_activation_journal")
        )
        if self._dialect == "mysql":
            rollback_ddl = rollback_ddl.replace(
                "id INTEGER PRIMARY KEY AUTOINCREMENT", "id BIGINT AUTO_INCREMENT PRIMARY KEY"
            )
            fdm_activation_ddl = fdm_activation_ddl.replace(
                "id INTEGER PRIMARY KEY AUTOINCREMENT",
                "id BIGINT AUTO_INCREMENT PRIMARY KEY",
            )
        cursor = self._conn.cursor()
        for statement in (
            generation_ddl,
            component_ddl,
            rollback_ddl,
            fdm_rollback_ddl,
            fdm_activation_ddl,
        ):
            cursor.execute(statement)
        self._conn.commit()

    def _table(self, table: str) -> str:
        if self._dialect == "sqlite":
            return table
        return f"`{self._schema_db}`.`{table}`"

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
            f"SELECT epoch, ingest_run_id, serving_db, generation_db "
            f"FROM {self._table('promotion_generation')} "
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
            f"INSERT INTO {self._table('promotion_generation')} "
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
            f"SELECT tables_json FROM {self._table('promotion_component')} "
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
            f"INSERT INTO {self._table('promotion_component')} "
            "(promotion_run_id, component, tables_json) VALUES (?, ?, ?)",
            (promotion_run_id, component, payload),
        )
        self._mark_good_when_complete(promotion_run_id)
        self._conn.commit()

    def _mark_good_when_complete(self, promotion_run_id: str) -> None:
        rows = self._execute(
            f"SELECT component FROM {self._table('promotion_component')} "
            "WHERE promotion_run_id=?",
            (promotion_run_id,),
        ).fetchall()
        components = {
            str(next(iter(row.values())) if isinstance(row, dict) else row[0])
            for row in rows
        }
        if components == REQUIRED_COMPONENTS:
            self._execute(
                f"UPDATE {self._table('promotion_generation')} "
                "SET status=? WHERE promotion_run_id=?",
                ("good", promotion_run_id),
            )

    def generation(self, target: str) -> PromotionGeneration | None:
        if target == "latest-good":
            cursor = self._execute(
                "SELECT promotion_run_id, epoch, ingest_run_id, serving_db, generation_db, status, promoted_at "
                f"FROM {self._table('promotion_generation')} "
                "WHERE status=? ORDER BY promoted_at DESC, promotion_run_id DESC LIMIT 1",
                ("good",),
            )
        else:
            cursor = self._execute(
                "SELECT promotion_run_id, epoch, ingest_run_id, serving_db, generation_db, status, promoted_at "
                f"FROM {self._table('promotion_generation')} WHERE promotion_run_id=?",
                (target,),
            )
        row = cursor.fetchone()
        return self._generation(row) if row is not None else None

    def generation_for_epoch(self, epoch: str) -> PromotionGeneration | None:
        row = self._execute(
            "SELECT promotion_run_id, epoch, ingest_run_id, serving_db, generation_db, status, promoted_at "
            f"FROM {self._table('promotion_generation')} WHERE epoch=? AND status=? "
            "ORDER BY promoted_at DESC, promotion_run_id DESC LIMIT 1",
            (epoch, "good"),
        ).fetchone()
        return self._generation(row) if row is not None else None

    def generations(self) -> tuple[PromotionGeneration, ...]:
        rows = self._execute(
            "SELECT promotion_run_id, epoch, ingest_run_id, serving_db, generation_db, status, promoted_at "
            f"FROM {self._table('promotion_generation')} "
            "ORDER BY promoted_at DESC, promotion_run_id DESC"
        ).fetchall()
        return tuple(self._generation(row) for row in rows)

    def components(self, promotion_run_id: str) -> dict[str, tuple[TableBackup, ...]]:
        rows = self._execute(
            f"SELECT component, tables_json FROM {self._table('promotion_component')} "
            "WHERE promotion_run_id=?",
            (promotion_run_id,),
        ).fetchall()
        result: dict[str, tuple[TableBackup, ...]] = {}
        for row in rows:
            component, raw = tuple(row.values()) if isinstance(row, dict) else tuple(row)
            result[str(component)] = tuple(TableBackup(**item) for item in json.loads(raw))
        return result

    def delete_component(self, promotion_run_id: str, component: str) -> None:
        self._execute(
            f"DELETE FROM {self._table('promotion_component')} "
            "WHERE promotion_run_id=? AND component=?",
            (promotion_run_id, component),
        )
        self._conn.commit()

    def record_rollback(self, promotion_run_id: str, *, actor: str, reason: str) -> None:
        rolled_back_at = _now()
        self._execute(
            f"INSERT INTO {self._table('promotion_rollback_event')} "
            "(promotion_run_id, actor, reason, rolled_back_at) "
            "VALUES (?, ?, ?, ?)",
            (promotion_run_id, actor, reason, rolled_back_at),
        )
        self._execute(
            f"UPDATE {self._table('promotion_generation')} "
            "SET status=? WHERE promotion_run_id=?",
            ("rolled_back", promotion_run_id),
        )
        self._conn.commit()

    def has_rollback_event(self, promotion_run_id: str) -> bool:
        row = self._execute(
            f"SELECT 1 FROM {self._table('promotion_rollback_event')} "
            "WHERE promotion_run_id=? LIMIT 1",
            (promotion_run_id,),
        ).fetchone()
        return row is not None

    def prepare_fdm_rollback(
        self,
        plan: FdmRollbackPlan,
        *,
        actor: str,
        reason: str,
    ) -> FdmRollbackState:
        current = self.fdm_rollback_state(plan.promotion_run_id)
        identity = (
            plan.target_db,
            plan.table.live_table,
            plan.table.backup_table,
            plan.failed_table,
            plan.table.expected_rows,
            plan.table.expected_digest,
            plan.pre_live_rows,
            plan.pre_live_digest,
            actor,
            reason,
        )
        if current is not None:
            existing = (
                current.target_db,
                current.live_table,
                current.backup_table,
                current.failed_table,
                current.expected_rows,
                current.expected_digest,
                current.pre_live_rows,
                current.pre_live_digest,
                current.actor,
                current.reason,
            )
            if existing != identity:
                raise RuntimeError(
                    f"FDM rollback journal identity conflict: {plan.promotion_run_id}"
                )
            if current.state == "compensated":
                self.update_fdm_rollback_state(plan.promotion_run_id, "prepared")
                refreshed = self.fdm_rollback_state(plan.promotion_run_id)
                if refreshed is None:
                    raise RuntimeError("FDM rollback journal disappeared after update")
                return refreshed
            return current
        self._execute(
            f"INSERT INTO {self._table('promotion_fdm_rollback_state')} "
            "(promotion_run_id, target_db, live_table, backup_table, failed_table, "
            "expected_rows, expected_digest, pre_live_rows, pre_live_digest, actor, "
            "reason, state, updated_at, last_error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                plan.promotion_run_id,
                *identity,
                "prepared",
                _now(),
                None,
            ),
        )
        self._conn.commit()
        created = self.fdm_rollback_state(plan.promotion_run_id)
        if created is None:
            raise RuntimeError("FDM rollback journal insert was not visible")
        return created

    def update_fdm_rollback_state(
        self,
        promotion_run_id: str,
        state: str,
        *,
        error: str | None = None,
    ) -> None:
        self._execute(
            f"UPDATE {self._table('promotion_fdm_rollback_state')} "
            "SET state=?, updated_at=?, last_error=? WHERE promotion_run_id=?",
            (state, _now(), error, promotion_run_id),
        )
        self._conn.commit()

    def fdm_rollback_state(self, promotion_run_id: str) -> FdmRollbackState | None:
        row = self._execute(
            "SELECT promotion_run_id, target_db, live_table, backup_table, "
            "failed_table, expected_rows, expected_digest, pre_live_rows, "
            "pre_live_digest, actor, reason, state, updated_at, last_error "
            f"FROM {self._table('promotion_fdm_rollback_state')} "
            "WHERE promotion_run_id=?",
            (promotion_run_id,),
        ).fetchone()
        if row is None:
            return None
        values = tuple(row.values()) if isinstance(row, dict) else tuple(row)
        return FdmRollbackState(*values)

    def record_fdm_activation_event(
        self,
        *,
        promotion_run_id: str,
        target_db: str,
        live_table: str,
        stage_table: str,
        backup_table: str,
        source: str,
        event: str,
        batch_index: int | None = None,
        rows_affected: int = 0,
        pre_live_rows: int | None = None,
        pre_live_digest: str | None = None,
        error: str | None = None,
    ) -> None:
        if not event.strip():
            raise ValueError("FDM activation journal event is required")
        identity = (
            target_db,
            live_table,
            stage_table,
            backup_table,
            source,
        )
        existing = self._execute(
            "SELECT target_db, live_table, stage_table, backup_table, source "
            f"FROM {self._table('promotion_fdm_activation_journal')} "
            "WHERE promotion_run_id=? ORDER BY id LIMIT 1",
            (promotion_run_id,),
        ).fetchone()
        if existing is not None:
            values = tuple(existing.values()) if isinstance(existing, dict) else tuple(existing)
            if values != identity:
                raise RuntimeError(
                    f"FDM activation journal identity conflict: {promotion_run_id}"
                )
        self._execute(
            f"INSERT INTO {self._table('promotion_fdm_activation_journal')} "
            "(promotion_run_id, target_db, live_table, stage_table, backup_table, "
            "source, event, batch_index, rows_affected, pre_live_rows, "
            "pre_live_digest, recorded_at, last_error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                promotion_run_id,
                *identity,
                event,
                batch_index,
                rows_affected,
                pre_live_rows,
                pre_live_digest,
                _now(),
                error,
            ),
        )
        self._conn.commit()

    def fdm_activation_events(
        self,
        promotion_run_id: str,
    ) -> tuple[FdmActivationEvent, ...]:
        rows = self._execute(
            "SELECT promotion_run_id, target_db, live_table, stage_table, "
            "backup_table, source, event, batch_index, rows_affected, "
            "pre_live_rows, pre_live_digest, recorded_at, last_error "
            f"FROM {self._table('promotion_fdm_activation_journal')} "
            "WHERE promotion_run_id=? ORDER BY id",
            (promotion_run_id,),
        ).fetchall()
        return tuple(
            FdmActivationEvent(
                *(tuple(row.values()) if isinstance(row, dict) else tuple(row))
            )
            for row in rows
        )

    def incomplete_fdm_activation_run_ids(self, target_db: str) -> tuple[str, ...]:
        rows = self._execute(
            "SELECT journal.promotion_run_id "
            f"FROM {self._table('promotion_fdm_activation_journal')} journal "
            "WHERE journal.target_db=? AND journal.id=("
            "SELECT MAX(latest.id) "
            f"FROM {self._table('promotion_fdm_activation_journal')} latest "
            "WHERE latest.promotion_run_id=journal.promotion_run_id"
            ") ORDER BY journal.promotion_run_id",
            (target_db,),
        ).fetchall()
        result: list[str] = []
        for row in rows:
            run_id = str(
                next(iter(row.values())) if isinstance(row, dict) else row[0]
            )
            events = self.fdm_activation_events(run_id)
            if events and events[-1].event not in _FDM_ACTIVATION_TERMINAL_EVENTS:
                result.append(run_id)
        return tuple(result)

    def rollback_events(self, promotion_run_id: str) -> tuple[RollbackEvent, ...]:
        rows = self._execute(
            "SELECT promotion_run_id, actor, reason, rolled_back_at "
            f"FROM {self._table('promotion_rollback_event')} "
            "WHERE promotion_run_id=? ORDER BY id",
            (promotion_run_id,),
        ).fetchall()
        return tuple(RollbackEvent(*tuple(row.values()) if isinstance(row, dict) else tuple(row)) for row in rows)

    def delete_component_for_test(self, promotion_run_id: str, component: str) -> None:
        if self._dialect != "sqlite":
            raise RuntimeError("test helper is restricted to sqlite")
        self._execute(
            f"DELETE FROM {self._table('promotion_component')} "
            "WHERE promotion_run_id=? AND component=?",
            (promotion_run_id, component),
        )
        self._conn.commit()

    @staticmethod
    def _generation(row: Any) -> PromotionGeneration:
        values = tuple(row.values()) if isinstance(row, dict) else tuple(row)
        return PromotionGeneration(*values)
