from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import pymysql

from pipeline.scripts.rollback.ledger import PromotionLedger
from pipeline.scripts.rollback.models import TableBackup

POST_GATE_STATEMENT_TIMEOUT_SECONDS = 5


class PostGateError(RuntimeError):
    """Base failure for a promotion post-gate decision."""


class PostGateRunAbsentError(PostGateError):
    """The requested ingest run has no ledger evidence."""


class PostGateRunIncompleteError(PostGateError):
    """The requested ingest run has not completed."""


class PostGateStatementTimeoutError(PostGateError):
    """The bounded post-gate SQL statement exceeded its deadline."""


class PostGateSocketTimeoutError(PostGateError):
    """The database socket timed out during the post-gate lookup."""


class PostGateLookupError(PostGateError):
    """The post-gate lookup failed for a non-timeout database reason."""


@dataclass(frozen=True, slots=True)
class PromotionIdentity:
    promotion_run_id: str
    epoch: str
    ingest_run_id: str
    serving_db: str
    generation_db: str
    ledger_db: str | None = None


class BackupInspector(Protocol):
    def exists(self, db_name: str, table_name: str) -> bool: ...

    def count(self, db_name: str, table_name: str) -> int: ...

    def digest(self, db_name: str, table_name: str) -> str: ...


def add_promotion_identity_args(parser: Any) -> None:
    parser.add_argument("--promotion-epoch")
    parser.add_argument("--ingest-run-id")
    parser.add_argument("--generation-db")
    parser.add_argument("--ledger-db")


def identity_from_args(
    args: Any,
    *,
    promotion_run_id: str,
    serving_db: str,
    required: bool = False,
) -> PromotionIdentity | None:
    values = (
        getattr(args, "promotion_epoch", None),
        getattr(args, "ingest_run_id", None),
        getattr(args, "generation_db", None),
        getattr(args, "ledger_db", None),
    )
    if not any(values) and required:
        raise ValueError(
            "promotion ledger identity is required: provide --promotion-epoch, "
            "--ingest-run-id, --generation-db, and --ledger-db"
        )
    if not any(values):
        return None
    if not all(values):
        raise ValueError(
            "promotion ledger wiring requires --promotion-epoch, --ingest-run-id, "
            "--generation-db, and --ledger-db together"
        )
    return PromotionIdentity(
        promotion_run_id=promotion_run_id,
        epoch=str(values[0]),
        ingest_run_id=str(values[1]),
        serving_db=serving_db,
        generation_db=str(values[2]),
        ledger_db=str(values[3]),
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

    ledger = PromotionLedger(
        conn,
        dialect="mysql",
        schema_db=identity.serving_db,
    )
    ledger.ensure_tables()
    return record_component_backups(
        ledger,
        MySQLMart(conn),
        identity=identity,
        component=component,
        table_pairs=table_pairs,
    )


def require_ingest_post_gate(
    conn: Any, identity: PromotionIdentity, *, dialect: str = "mysql"
) -> None:
    """Refuse promotion unless its linked ingest run completed every post-gate."""
    if not identity.ledger_db:
        raise ValueError("promotion post-gate requires an explicit ledger_db")
    ledger_db = _qualified_ledger_db(identity.ledger_db, dialect=dialect)
    mark = "?" if dialect == "sqlite" else "%s"
    cursor = conn.cursor()
    query = (
        f"SELECT status, reason FROM {ledger_db}.ingest_ledger "
        f"WHERE run_id={mark} ORDER BY id DESC LIMIT 1"
    )
    if dialect == "mysql":
        query = (
            "SET STATEMENT "
            f"max_statement_time={POST_GATE_STATEMENT_TIMEOUT_SECONDS} FOR "
            f"{query}"
        )
    try:
        cursor.execute(query, (identity.ingest_run_id,))
    except pymysql.err.OperationalError as exc:
        code = int(exc.args[0]) if exc.args else 0
        detail = str(exc.args[1]) if len(exc.args) > 1 else str(exc)
        if code in {1969, 3024}:
            raise PostGateStatementTimeoutError(
                "promotion blocked: post-gate statement timed out after "
                f"{POST_GATE_STATEMENT_TIMEOUT_SECONDS}s"
            ) from exc
        if code == 2013 and "timed out" in detail.lower():
            raise PostGateSocketTimeoutError(
                "promotion blocked: post-gate socket timed out"
            ) from exc
        raise PostGateLookupError(
            f"promotion blocked: post-gate lookup failed: mysql_error={code}"
        ) from exc
    except pymysql.MySQLError as exc:
        raise PostGateLookupError(
            "promotion blocked: post-gate lookup failed"
        ) from exc
    row = cursor.fetchone()
    if row is None:
        raise PostGateRunAbsentError(
            f"promotion blocked: ingest_run_id={identity.ingest_run_id} is absent from ingest_ledger"
        )
    values = tuple(row.values()) if isinstance(row, dict) else tuple(row)
    status, reason = str(values[0]), values[1]
    if status != "complete":
        raise PostGateRunIncompleteError(
            f"promotion blocked: ingest_run_id={identity.ingest_run_id} status={status} "
            f"reason={reason}; rollback=python -m pipeline.scripts.rollback "
            "--to latest-good --dry-run"
        )


def _qualified_ledger_db(ledger_db: str, *, dialect: str) -> str:
    if not ledger_db.replace("_", "").isalnum():
        raise ValueError(f"unsafe ledger schema name: {ledger_db}")
    if dialect == "sqlite":
        return f'"{ledger_db}"'
    return f"`{ledger_db}`"
