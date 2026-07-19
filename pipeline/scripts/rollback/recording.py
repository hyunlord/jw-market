from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from pipeline.scripts.rollback.ledger import PromotionLedger
from pipeline.scripts.rollback.models import TableBackup


@dataclass(frozen=True, slots=True)
class PromotionIdentity:
    promotion_run_id: str
    epoch: str
    ingest_run_id: str
    serving_db: str
    generation_db: str


class BackupInspector(Protocol):
    def exists(self, db_name: str, table_name: str) -> bool: ...

    def count(self, db_name: str, table_name: str) -> int: ...

    def digest(self, db_name: str, table_name: str) -> str: ...


def add_promotion_identity_args(parser: Any) -> None:
    parser.add_argument("--promotion-epoch")
    parser.add_argument("--ingest-run-id")
    parser.add_argument("--generation-db")


def identity_from_args(
    args: Any,
    *,
    promotion_run_id: str,
    serving_db: str,
) -> PromotionIdentity | None:
    values = (
        getattr(args, "promotion_epoch", None),
        getattr(args, "ingest_run_id", None),
        getattr(args, "generation_db", None),
    )
    if not any(values):
        return None
    if not all(values):
        raise ValueError(
            "promotion ledger wiring requires --promotion-epoch, --ingest-run-id, "
            "and --generation-db together"
        )
    return PromotionIdentity(
        promotion_run_id=promotion_run_id,
        epoch=str(values[0]),
        ingest_run_id=str(values[1]),
        serving_db=serving_db,
        generation_db=str(values[2]),
    )


def record_component_backups(
    ledger: PromotionLedger,
    inspector: BackupInspector,
    *,
    identity: PromotionIdentity,
    component: str,
    table_pairs: tuple[tuple[str, str], ...],
) -> tuple[TableBackup, ...]:
    if not table_pairs:
        raise RuntimeError(f"promotion component has no rollback backups: {component}")
    backups: list[TableBackup] = []
    for live_table, backup_table in table_pairs:
        if not inspector.exists(identity.serving_db, backup_table):
            raise RuntimeError(
                f"promotion backup missing: {identity.serving_db}.{backup_table}"
            )
        rows = inspector.count(identity.serving_db, backup_table)
        if rows < 1:
            raise RuntimeError(
                f"promotion backup is empty: {identity.serving_db}.{backup_table}"
            )
        backups.append(
            TableBackup(
                live_table=live_table,
                backup_table=backup_table,
                expected_rows=rows,
                expected_digest=inspector.digest(identity.serving_db, backup_table),
            )
        )
    result = tuple(backups)
    ledger.record_component(
        promotion_run_id=identity.promotion_run_id,
        component=component,
        epoch=identity.epoch,
        ingest_run_id=identity.ingest_run_id,
        target_db=identity.serving_db,
        generation_db=identity.generation_db,
        tables=result,
    )
    return result


def record_mysql_component(
    conn: Any,
    *,
    identity: PromotionIdentity,
    component: str,
    table_pairs: tuple[tuple[str, str], ...],
) -> tuple[TableBackup, ...]:
    from pipeline.scripts.rollback.mysql_ops import MySQLMart

    ledger = PromotionLedger(conn, dialect="mysql")
    ledger.ensure_tables()
    return record_component_backups(
        ledger,
        MySQLMart(conn),
        identity=identity,
        component=component,
        table_pairs=table_pairs,
    )
