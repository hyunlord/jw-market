"""Stored-procedure activation for IQVIA CSD channel data.

The activator account has EXECUTE only. Candidate mutation, validation, publish,
rollback cleanup, and abandon therefore cross the database boundary exclusively
through the five ``jw_csd_channel_control`` routines.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import json
from pathlib import Path
import re
from typing import Any, Final, TypeVar

from pipeline.scripts.etl.brand_activity.csd_core import CsdRow, source_month_key
from pipeline.scripts.etl.brand_activity.km_core import source_sha256
from pipeline.scripts.etl.brand_activity.raw_db import csd_raw_record
from pipeline.scripts.etl.brand_activity.raw_extract import read_csd_source_rows
from pipeline.scripts.etl.brand_activity.raw_stage_refresh import _stage_loaded_at


COPY_BATCH_ROWS: Final = 1000
RAW_SCHEMA: Final = "jw_brand_activity_raw_stage"
RAW_TABLE: Final = "raw_csd_channel_dynamics"
STAGE_SCHEMA: Final = "jw_brand_activity_stage"
STAGE_TABLE: Final = "csd_channel_dynamics_stage"
CONTROL_SCHEMA: Final = "jw_csd_channel_control"
WRITER_LOCK_NAME: Final = "jw_ingest_csd_channel_activation"
ROLLBACK_RETENTION_DAYS: Final = 7
ROLLBACK_SNAPSHOTS_TO_KEEP: Final = 1
_RUN_ID = re.compile(r"^[0-9]{20}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+$")
_T = TypeVar("_T")


class CandidateValidationError(RuntimeError):
    pass


class AmbiguousPublishError(RuntimeError):
    pass


class SwapVerdict(StrEnum):
    NOT_APPLIED = "not_applied"
    APPLIED = "applied"


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
    raw: TablePair
    stage: TablePair

    def pairs(self) -> tuple[TablePair, TablePair]:
        return self.raw, self.stage

    def table_refs(self) -> tuple[TableRef, ...]:
        return tuple(ref for pair in self.pairs() for ref in (pair.live, pair.candidate, pair.rollback))


@dataclass(frozen=True, slots=True)
class TableFingerprint:
    row_count: int
    crc_sum: int
    crc_xor: int


@dataclass(frozen=True, slots=True)
class PeriodContract:
    months: int
    complete_quarters: tuple[str, ...]
    excluded_boundary_months: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ChannelAggregate:
    period_ym: str
    market: str
    master_product: str
    representing_company: str
    channel: str
    value: int


@dataclass(frozen=True, slots=True)
class ChannelGateResult:
    groups_checked: int


@dataclass(frozen=True, slots=True)
class CandidateEvidence:
    raw: TableFingerprint
    stage: TableFingerprint
    live_raw: TableFingerprint
    live_stage: TableFingerprint
    periods: PeriodContract
    channels: ChannelGateResult
    commits: int


def _safe_identifier(value: str) -> str:
    if _IDENTIFIER.fullmatch(value) is None or len(value) > 64:
        raise ValueError(f"unsafe MariaDB identifier: {value!r}")
    return value


def _valid_run_id(run_id: str) -> str:
    if _RUN_ID.fullmatch(run_id) is None:
        raise ValueError("run_id must contain exactly 20 digits")
    return run_id


def plan_for_run(
    run_id: str,
    *,
    created_at: datetime | None = None,
    raw_schema: str = RAW_SCHEMA,
    stage_schema: str = STAGE_SCHEMA,
) -> ActivationPlan:
    del created_at
    run_id = _valid_run_id(run_id)
    raw_schema = _safe_identifier(raw_schema)
    stage_schema = _safe_identifier(stage_schema)
    if (raw_schema, stage_schema) != (RAW_SCHEMA, STAGE_SCHEMA):
        raise ValueError("CSD wrapper supports only the canonical live scope")
    return ActivationPlan(
        run_id,
        TablePair(
            TableRef(raw_schema, RAW_TABLE),
            TableRef(f"{raw_schema}_csd_{run_id}", RAW_TABLE),
            TableRef("jw_csd_channel_rollback_raw", f"{RAW_TABLE}__rollback_{run_id}"),
        ),
        TablePair(
            TableRef(stage_schema, STAGE_TABLE),
            TableRef(f"{stage_schema}_csd_{run_id}", STAGE_TABLE),
            TableRef("jw_csd_channel_rollback_stage", f"{STAGE_TABLE}__rollback_{run_id}"),
        ),
    )


def plan_payload(plan: ActivationPlan) -> dict[str, object]:
    return {
        "run_id": plan.run_id,
        "raw": _pair_payload(plan.raw),
        "stage": _pair_payload(plan.stage),
    }


def _pair_payload(pair: TablePair) -> dict[str, dict[str, str]]:
    return {
        role: {"schema": ref.schema, "table": ref.table}
        for role, ref in (("live", pair.live), ("candidate", pair.candidate), ("rollback", pair.rollback))
    }


def plan_from_payload(payload: dict[str, object]) -> ActivationPlan:
    run_id = _valid_run_id(str(payload.get("run_id") or ""))

    def pair(name: str) -> TablePair:
        item = payload.get(name)
        if not isinstance(item, dict):
            raise CandidateValidationError(f"activation payload has no {name} table pair")

        def ref(role: str) -> TableRef:
            value = item.get(role)
            if not isinstance(value, dict):
                raise CandidateValidationError(f"activation payload has no {name}.{role}")
            return TableRef(
                _safe_identifier(str(value.get("schema") or "")),
                _safe_identifier(str(value.get("table") or "")),
            )

        return TablePair(ref("live"), ref("candidate"), ref("rollback"))

    return ActivationPlan(run_id, pair("raw"), pair("stage"))


def validate_plan_scope(
    plan: ActivationPlan,
    *,
    expected_run_id: str,
    raw_schema: str,
    stage_schema: str,
) -> None:
    expected = plan_for_run(expected_run_id, raw_schema=raw_schema, stage_schema=stage_schema)
    if plan.run_id != expected.run_id:
        raise CandidateValidationError("activation run identity mismatch")
    if (plan.raw.live, plan.stage.live) != (expected.raw.live, expected.stage.live):
        raise CandidateValidationError("activation live table scope mismatch")
    if (plan.raw.candidate, plan.stage.candidate) != (expected.raw.candidate, expected.stage.candidate):
        raise CandidateValidationError("activation candidate scope mismatch")
    if (plan.raw.rollback, plan.stage.rollback) != (expected.raw.rollback, expected.stage.rollback):
        raise CandidateValidationError("activation rollback scope mismatch")


def _fingerprint_payload(value: TableFingerprint) -> dict[str, int]:
    return {"row_count": value.row_count, "crc_sum": value.crc_sum, "crc_xor": value.crc_xor}


def evidence_payload(evidence: CandidateEvidence) -> dict[str, object]:
    return {
        "raw": _fingerprint_payload(evidence.raw),
        "stage": _fingerprint_payload(evidence.stage),
        "live_raw": _fingerprint_payload(evidence.live_raw),
        "live_stage": _fingerprint_payload(evidence.live_stage),
        "months": evidence.periods.months,
        "complete_quarters": list(evidence.periods.complete_quarters),
        "excluded_boundary_months": list(evidence.periods.excluded_boundary_months),
        "channel_groups_checked": evidence.channels.groups_checked,
        "commits": evidence.commits,
    }


def evidence_from_payload(payload: dict[str, object]) -> CandidateEvidence:
    def fingerprint(name: str) -> TableFingerprint:
        value = payload.get(name)
        if not isinstance(value, dict):
            raise CandidateValidationError(f"activation evidence has no {name} fingerprint")
        return TableFingerprint(int(value.get("row_count", -1)), int(value.get("crc_sum", -1)), int(value.get("crc_xor", -1)))

    return CandidateEvidence(
        fingerprint("raw"),
        fingerprint("stage"),
        fingerprint("live_raw"),
        fingerprint("live_stage"),
        PeriodContract(
            int(payload.get("months", -1)),
            tuple(str(item) for item in payload.get("complete_quarters", [])),
            tuple(str(item) for item in payload.get("excluded_boundary_months", [])),
        ),
        ChannelGateResult(int(payload.get("channel_groups_checked", -1))),
        int(payload.get("commits", 0)),
    )


def batches(values: Sequence[_T], batch_size: int = COPY_BATCH_ROWS) -> Iterator[Sequence[_T]]:
    if batch_size < 1 or batch_size > COPY_BATCH_ROWS:
        raise ValueError(f"batch_size must be between 1 and {COPY_BATCH_ROWS}")
    for start in range(0, len(values), batch_size):
        yield values[start : start + batch_size]


def month_range(start: str, end: str) -> tuple[str, ...]:
    start_year, start_month = map(int, start.split("-"))
    end_year, end_month = map(int, end.split("-"))
    result: list[str] = []
    year, month = start_year, start_month
    while (year, month) <= (end_year, end_month):
        result.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year += 1
            month = 1
    return tuple(result)


def validate_period_contract(periods: Iterable[str]) -> PeriodContract:
    unique = tuple(sorted(set(periods)))
    if len(unique) != 36 or not unique or unique != month_range(unique[0], unique[-1]):
        raise CandidateValidationError(f"stage must contain continuous 36 months; observed={len(unique)}")
    by_quarter: dict[tuple[int, int], list[str]] = defaultdict(list)
    for period in unique:
        year, month = map(int, period.split("-"))
        by_quarter[(year, (month - 1) // 3 + 1)].append(period)
    complete = tuple(f"{year}-Q{quarter}" for (year, quarter), months in sorted(by_quarter.items()) if len(months) == 3)
    boundary = tuple(month for key in (min(by_quarter), max(by_quarter)) if len(by_quarter[key]) < 3 for month in by_quarter[key])
    incomplete_internal = [f"{year}-Q{quarter}" for (year, quarter), months in sorted(by_quarter.items())[1:-1] if len(months) != 3]
    if incomplete_internal:
        raise CandidateValidationError(f"incomplete internal quarters: {incomplete_internal}")
    return PeriodContract(len(unique), complete, boundary)


def validate_channel_totals(rows: Iterable[ChannelAggregate]) -> ChannelGateResult:
    grouped: dict[tuple[str, str, str, str], dict[str, int]] = defaultdict(dict)
    for row in rows:
        grouped[(row.period_ym, row.market, row.master_product, row.representing_company)][row.channel] = row.value
    for key, channels in grouped.items():
        required = {"TOTAL", "GH", "SHPPI", "CPPI", "GH+SHPPI"}
        missing = sorted(required.difference(channels))
        if missing:
            raise CandidateValidationError(f"channel gate missing {missing} for {key}")
        if channels["TOTAL"] != channels["GH"] + channels["SHPPI"] + channels["CPPI"]:
            raise CandidateValidationError(f"TOTAL channel mismatch for {key}")
        if channels["GH+SHPPI"] != channels["GH"] + channels["SHPPI"]:
            raise CandidateValidationError(f"GH+SHPPI channel mismatch for {key}")
    return ChannelGateResult(len(grouped))


def _call_rows(conn: Any, routine: str, params: tuple[object, ...]) -> list[dict[str, object]]:
    placeholders = ",".join(["%s"] * len(params))
    sql = f"CALL `{CONTROL_SCHEMA}`.`{routine}`({placeholders})"
    with conn.cursor() as cursor:
        cursor.execute(sql, params)
        rows = list(cursor.fetchall())
        nextset = getattr(cursor, "nextset", None)
        if nextset is not None:
            while nextset():
                pass
    return rows


def _load_action(
    conn: Any,
    plan: ActivationPlan,
    action: str,
    *,
    after_id: int = 0,
    window: tuple[str, str] | None = None,
    rows: Sequence[dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    start, end = window or (None, None)
    payload = json.dumps(rows, ensure_ascii=True, separators=(",", ":"), default=str) if rows is not None else None
    return _call_rows(conn, "csd_candidate_load", (plan.run_id, action, after_id, start, end, payload))


def _fingerprints_from_validate(rows: Sequence[dict[str, object]]) -> tuple[TableFingerprint, TableFingerprint]:
    by_kind = {str(row["candidate_kind"]): row for row in rows}
    if set(by_kind) != {"raw", "stage"}:
        raise CandidateValidationError("wrapper validate did not return the raw/stage pair")

    def value(kind: str) -> TableFingerprint:
        row = by_kind[kind]
        return TableFingerprint(int(row["row_count"]), int(row["crc_sum"]), int(row["crc_xor"]))

    return value("raw"), value("stage")


def _uploaded_batches(source_paths: Sequence[Path]) -> Iterator[Sequence[dict[str, object]]]:
    records = [
        csd_raw_record(row)
        for source in source_paths
        for row in read_csd_source_rows(source, source_sha256(source))
    ]
    yield from batches(records)


def _read_stage_source(conn: Any, plan: ActivationPlan) -> list[CsdRow]:
    after_id = 0
    source_rows: list[dict[str, object]] = []
    while True:
        page = _load_action(conn, plan, "read_stage_source", after_id=after_id, window=("0001-01", "9999-12"))
        if not page:
            break
        source_rows.extend(page)
        next_id = int(page[-1]["id"])
        if next_id <= after_id:
            raise CandidateValidationError("read_stage_source did not advance")
        after_id = next_id
    grouped: dict[tuple[str, str, str, str, str], CsdRow] = {}
    for raw in source_rows:
        row = CsdRow(
            source_file=str(raw["source_file"]),
            source_sheet=str(raw["source_sheet"]),
            source_row_no=int(raw["source_row_no"]),
            period_ym=str(raw["period_ym"]),
            market=str(raw["market"]),
            jw_channel=str(raw["jw_channel"]),
            master_product=str(raw["master_product"]),
            representing_company=str(raw["representing_company"]),
            product_details=int(raw["product_details"]),
        )
        current = grouped.get(row.grain_key())
        if current is None or source_month_key(row.source_file) > source_month_key(current.source_file):
            grouped[row.grain_key()] = row
    if not grouped:
        return []
    end = max(row.period_ym for row in grouped.values())
    end_year, end_month = map(int, end.split("-"))
    start_index = end_year * 12 + end_month - 36
    start = f"{start_index // 12:04d}-{start_index % 12 + 1:02d}"
    return sorted((row for row in grouped.values() if start <= row.period_ym <= end), key=lambda row: row.grain_key())


def _stage_json(rows: Sequence[CsdRow]) -> list[dict[str, object]]:
    loaded_at = _stage_loaded_at(max(row.period_ym for row in rows))
    return [{**row.to_dict(), "loaded_at": loaded_at} for row in rows]


def _gate_stage(rows: Sequence[CsdRow]) -> tuple[PeriodContract, ChannelGateResult]:
    periods = validate_period_contract(row.period_ym for row in rows)
    grouped: dict[tuple[str, str, str, str, str], int] = defaultdict(int)
    for row in rows:
        grouped[(row.period_ym, row.market, row.master_product, row.representing_company, row.jw_channel)] += row.product_details
    channels = validate_channel_totals(ChannelAggregate(*key[:4], key[4], value) for key, value in grouped.items())
    return periods, channels


def stage_gate_evidence(
    rows: Sequence[CsdRow], *, enforce: bool
) -> tuple[PeriodContract, ChannelGateResult]:
    """Observe candidate coverage while making commissioning post-gates a no-op."""
    if enforce:
        return _gate_stage(rows)
    periods = tuple(sorted({row.period_ym for row in rows}))
    return PeriodContract(len(periods), (), ()), ChannelGateResult(0)


def prepare_candidate(
    conn: Any,
    plan: ActivationPlan,
    *,
    source_paths: Sequence[Path],
    enforce_post_gate: bool = True,
) -> CandidateEvidence:
    """Build and validate a candidate using only wrapper mutations."""
    created = False
    try:
        _call_rows(conn, "csd_candidate_create", (plan.run_id,))
        created = True
        live_raw, live_stage = validate_live(conn, plan)
        commits = 0
        for batch in _uploaded_batches(source_paths):
            _load_action(conn, plan, "merge_uploaded_raw", rows=batch)
            commits += 1
        candidate_rows = _read_stage_source(conn, plan)
        periods, channels = stage_gate_evidence(
            candidate_rows, enforce=enforce_post_gate
        )
        for batch in batches(_stage_json(candidate_rows)):
            result = _load_action(conn, plan, "append_stage", rows=batch)
            if int(result[0]["affected_rows"]) != len(batch):
                raise CandidateValidationError("append_stage affected row count mismatch")
            commits += 1
        raw, stage = _fingerprints_from_validate(_load_action(conn, plan, "validate"))
        if raw.row_count <= 0 or stage.row_count <= 0:
            raise CandidateValidationError(f"candidate row count is empty: raw={raw.row_count} stage={stage.row_count}")
        return CandidateEvidence(raw, stage, live_raw, live_stage, periods, channels, commits)
    except Exception:
        if created:
            abandon_candidate(conn, plan)
        raise


def validate_candidate(conn: Any, plan: ActivationPlan, recorded: CandidateEvidence) -> CandidateEvidence:
    raw, stage = _fingerprints_from_validate(_load_action(conn, plan, "validate"))
    return CandidateEvidence(raw, stage, recorded.live_raw, recorded.live_stage, recorded.periods, recorded.channels, recorded.commits)


def validate_live(conn: Any, plan: ActivationPlan) -> tuple[TableFingerprint, TableFingerprint]:
    """Read the live pair through the wrapper's publish-canonical SQL expression."""
    return _fingerprints_from_validate(_load_action(conn, plan, "validate_live"))


def publish_candidate(conn: Any, plan: ActivationPlan, evidence: CandidateEvidence) -> SwapVerdict:
    result = _call_rows(
        conn,
        "csd_atomic_publish",
        (
            plan.run_id,
            evidence.raw.row_count, evidence.raw.crc_sum, evidence.raw.crc_xor,
            evidence.stage.row_count, evidence.stage.crc_sum, evidence.stage.crc_xor,
            evidence.live_raw.row_count, evidence.live_raw.crc_sum, evidence.live_raw.crc_xor,
            evidence.live_stage.row_count, evidence.live_stage.crc_sum, evidence.live_stage.crc_xor,
        ),
    )
    state = str(result[0].get("publish_state") if result else "")
    if state not in {"applied", "applied_observed"}:
        raise AmbiguousPublishError(f"wrapper returned unknown publish state: {state!r}")
    return SwapVerdict.APPLIED


def cleanup_rollback(conn: Any, plan: ActivationPlan, evidence: CandidateEvidence) -> None:
    _call_rows(
        conn,
        "csd_rollback_cleanup",
        (plan.run_id, evidence.live_raw.row_count, evidence.live_stage.row_count, "DROP_EXACT_ROLLBACK_PAIR"),
    )


def abandon_candidate(conn: Any, plan: ActivationPlan) -> None:
    _call_rows(conn, "csd_candidate_abandon", (plan.run_id, "ABANDON_UNPUBLISHED_CANDIDATE"))
