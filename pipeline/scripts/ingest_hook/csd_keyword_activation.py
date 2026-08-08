"""Run-scoped IQVIA keyword activation with an atomic raw+stage swap."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any


RAW_SCHEMA = "jw_brand_activity_raw_stage"
RAW_TABLE = "raw_keyword_events"
STAGE_SCHEMA = "jw_brand_activity_stage"
STAGE_TABLE = "km_keyword_event_stage"
WRITER_LOCK_NAME = "jw_ingest_csd_keyword_activation"

_RUN_ID = re.compile(r"^[0-9]{20}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+$")


class CandidateValidationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TableRef:
    schema: str
    table: str


@dataclass(frozen=True, slots=True)
class TablePair:
    live: TableRef
    candidate: TableRef
    rollback: TableRef


@dataclass(frozen=True, slots=True)
class ActivationPlan:
    run_id: str
    candidate_base: str
    raw: TablePair
    stage: TablePair

    def table_refs(self) -> tuple[TableRef, ...]:
        return tuple(
            ref
            for pair in (self.raw, self.stage)
            for ref in (pair.live, pair.candidate, pair.rollback)
        )


@dataclass(frozen=True, slots=True)
class CandidateEvidence:
    raw_rows: int
    stage_rows: int
    period_count: int
    min_period: str
    max_period: str


def _identifier(value: str) -> str:
    if _IDENTIFIER.fullmatch(value) is None or len(value) > 64:
        raise ValueError(f"unsafe MariaDB identifier: {value!r}")
    return value


def _run_id(value: str) -> str:
    if _RUN_ID.fullmatch(value) is None:
        raise ValueError("run_id must contain exactly 20 digits")
    return value


def plan_for_run(run_id: str) -> ActivationPlan:
    run_id = _run_id(run_id)
    candidate_base = _identifier(f"jw_brand_activity_keyword_{run_id}")
    return ActivationPlan(
        run_id=run_id,
        candidate_base=candidate_base,
        raw=TablePair(
            live=TableRef(RAW_SCHEMA, RAW_TABLE),
            candidate=TableRef(f"{candidate_base}_raw", RAW_TABLE),
            rollback=TableRef(RAW_SCHEMA, f"{RAW_TABLE}__old_{run_id}"),
        ),
        stage=TablePair(
            live=TableRef(STAGE_SCHEMA, STAGE_TABLE),
            candidate=TableRef(f"{candidate_base}_stage", STAGE_TABLE),
            rollback=TableRef(STAGE_SCHEMA, f"{STAGE_TABLE}__old_{run_id}"),
        ),
    )


def _ref_payload(ref: TableRef) -> dict[str, str]:
    return {"schema": ref.schema, "table": ref.table}


def plan_payload(plan: ActivationPlan) -> dict[str, object]:
    return {
        "run_id": plan.run_id,
        "candidate_base": plan.candidate_base,
        "raw": {
            "live": _ref_payload(plan.raw.live),
            "candidate": _ref_payload(plan.raw.candidate),
            "rollback": _ref_payload(plan.raw.rollback),
        },
        "stage": {
            "live": _ref_payload(plan.stage.live),
            "candidate": _ref_payload(plan.stage.candidate),
            "rollback": _ref_payload(plan.stage.rollback),
        },
    }


def plan_from_payload(payload: dict[str, object]) -> ActivationPlan:
    expected = plan_for_run(str(payload.get("run_id") or ""))
    if plan_payload(expected) != payload:
        raise CandidateValidationError("keyword activation plan is not canonical")
    return expected


def evidence_payload(evidence: CandidateEvidence) -> dict[str, object]:
    return {
        "raw_rows": evidence.raw_rows,
        "stage_rows": evidence.stage_rows,
        "period_count": evidence.period_count,
        "min_period": evidence.min_period,
        "max_period": evidence.max_period,
    }


def evidence_from_payload(payload: dict[str, object]) -> CandidateEvidence:
    return CandidateEvidence(
        raw_rows=int(payload.get("raw_rows", -1)),
        stage_rows=int(payload.get("stage_rows", -1)),
        period_count=int(payload.get("period_count", -1)),
        min_period=str(payload.get("min_period") or ""),
        max_period=str(payload.get("max_period") or ""),
    )


def _qualified(ref: TableRef) -> str:
    return f"`{_identifier(ref.schema)}`.`{_identifier(ref.table)}`"


def _table_exists(conn: Any, ref: TableRef) -> bool:
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) AS n FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s",
            (ref.schema, ref.table),
        )
        row = cursor.fetchone()
    if isinstance(row, dict):
        return int(row.get("n", 0)) == 1
    return bool(row and int(row[0]) == 1)


def validate_candidate(conn: Any, plan: ActivationPlan) -> CandidateEvidence:
    for ref in (plan.raw.candidate, plan.stage.candidate):
        if not _table_exists(conn, ref):
            raise CandidateValidationError(f"candidate table is absent: {ref.schema}.{ref.table}")
    with conn.cursor() as cursor:
        cursor.execute(f"SELECT COUNT(*) AS n FROM {_qualified(plan.raw.candidate)}")
        raw_row = cursor.fetchone()
        cursor.execute(
            f"SELECT COUNT(*) AS n, COUNT(DISTINCT period_ym) AS period_count, "
            f"MIN(period_ym) AS min_period, MAX(period_ym) AS max_period "
            f"FROM {_qualified(plan.stage.candidate)}"
        )
        stage_row = cursor.fetchone()
    raw_rows = int(raw_row["n"] if isinstance(raw_row, dict) else raw_row[0])
    if isinstance(stage_row, dict):
        evidence = CandidateEvidence(
            raw_rows,
            int(stage_row["n"]),
            int(stage_row["period_count"]),
            str(stage_row["min_period"] or ""),
            str(stage_row["max_period"] or ""),
        )
    else:
        evidence = CandidateEvidence(
            raw_rows,
            int(stage_row[0]),
            int(stage_row[1]),
            str(stage_row[2] or ""),
            str(stage_row[3] or ""),
        )
    if evidence.raw_rows < 1 or evidence.stage_rows < 1:
        raise CandidateValidationError(f"keyword candidate is empty: {evidence}")
    return evidence


def require_publish_scope(conn: Any, plan: ActivationPlan) -> None:
    for ref in (plan.raw.live, plan.stage.live, plan.raw.candidate, plan.stage.candidate):
        if not _table_exists(conn, ref):
            raise CandidateValidationError(f"publish table is absent: {ref.schema}.{ref.table}")
    for ref in (plan.raw.rollback, plan.stage.rollback):
        if _table_exists(conn, ref):
            raise CandidateValidationError(f"rollback table already exists: {ref.schema}.{ref.table}")


def publish_candidate(conn: Any, plan: ActivationPlan) -> None:
    """Atomically preserve both live tables and promote both candidates."""
    sql = "RENAME TABLE " + ", ".join(
        (
            f"{_qualified(plan.raw.live)} TO {_qualified(plan.raw.rollback)}",
            f"{_qualified(plan.raw.candidate)} TO {_qualified(plan.raw.live)}",
            f"{_qualified(plan.stage.live)} TO {_qualified(plan.stage.rollback)}",
            f"{_qualified(plan.stage.candidate)} TO {_qualified(plan.stage.live)}",
        )
    )
    with conn.cursor() as cursor:
        cursor.execute(sql)
    conn.commit()
