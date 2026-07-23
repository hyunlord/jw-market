"""Fail-closed post-ingest gates shared by rehearsal and mart runners."""
from __future__ import annotations

import json
import hashlib
import random
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SigmaEvidence:
    checked: int
    population: int
    detail: str


@dataclass(frozen=True, slots=True)
class TableFingerprint:
    table: str
    row_count: int
    sample_sha256: str


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    tables: tuple[TableFingerprint, ...]


@dataclass(frozen=True, slots=True)
class GateResult:
    gate: str
    status: str
    checked: int
    population: int
    tolerance: str
    detail: str


@dataclass(frozen=True, slots=True)
class PostGateReport:
    run_id: str
    epoch: str
    category: str
    status: str
    duration_ms: float
    rollback_command: str
    gates: tuple[GateResult, ...]


class PostGateError(RuntimeError):
    def __init__(self, report: PostGateReport, report_path: Path):
        failures = [gate for gate in report.gates if gate.status == "fail"]
        detail = "; ".join(f"{gate.gate}: {gate.detail}" for gate in failures)
        super().__init__(f"post-ingest gate failed ({detail}); report={report_path}")
        self.report = report
        self.report_path = report_path


def _first_value(row):
    return next(iter(row.values())) if isinstance(row, dict) else row[0]


def fingerprint_untouched_sources(conn, *, touched_source: str, mark: str = "%s") -> SourceSnapshot:
    """Fingerprint non-target rows without trusting storage-engine estimates."""
    specs = (
        (
            "mart_general_market_metric",
            "source, measure, atc4_code, market_size_series",
            "source, measure, atc4_code",
        ),
        (
            "mart_general_brand_metric",
            "source, measure, atc4_code, brand_key, metric_history",
            "source, measure, atc4_code, brand_key",
        ),
    )
    results: list[TableFingerprint] = []
    cursor = conn.cursor()
    for table, columns, order_by in specs:
        cursor.execute(f"SELECT COUNT(*) FROM {table} WHERE source <> {mark}", (touched_source,))
        row = cursor.fetchone()
        count = int(_first_value(row))
        cursor.execute(
            f"SELECT {columns} FROM {table} WHERE source <> {mark} "
            f"ORDER BY {order_by} LIMIT 128",
            (touched_source,),
        )
        sample = cursor.fetchall()
        digest = hashlib.sha256(
            json.dumps(sample, ensure_ascii=False, default=str, separators=(",", ":")).encode()
        ).hexdigest()
        results.append(TableFingerprint(table, count, digest))
    return SourceSnapshot(tuple(results))


def sample_existing_periods(
    conn,
    *,
    source: str,
    excluded: tuple[str, ...],
    sample_size: int = 3,
    mark: str = "%s",
) -> tuple[str, ...]:
    """Choose a reproducible random sample of historical market periods."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT market_size_series FROM mart_general_market_metric"
        f" WHERE source={mark} AND measure='sales'",
        (source,),
    )
    periods: set[str] = set()
    for row in cursor.fetchall():
        raw = _first_value(row)
        try:
            value = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            value = {}
        if isinstance(value, dict):
            periods.update(str(period) for period in value)
    candidates = sorted(periods.difference(excluded))
    if not candidates:
        return ()
    rng = random.Random(f"{source}:{','.join(sorted(excluded))}")
    return tuple(sorted(rng.sample(candidates, min(sample_size, len(candidates)))))


def staging_row_count(conn, table: str) -> int:
    cursor = conn.cursor()
    cursor.execute(f"SELECT COUNT(*) FROM {table}")  # noqa: S608 - internal table name
    row = cursor.fetchone()
    return int(_first_value(row))


def run_post_gates(
    *,
    run_id: str,
    epoch: str,
    category: str,
    sigma_check: Callable[[], SigmaEvidence],
    expected_rows: int,
    actual_rows: int,
    untouched_before: SourceSnapshot,
    untouched_after: SourceSnapshot,
    report_path: Path,
) -> PostGateReport:
    """Run all gates, persist evidence, then raise if any gate failed."""
    started = time.monotonic()
    gates: list[GateResult] = []
    try:
        sigma = sigma_check()
        if sigma.population < 1 or sigma.checked != sigma.population:
            raise ValueError(
                f"incomplete sigma census checked={sigma.checked} population={sigma.population}"
            )
        gates.append(
            GateResult("PG-1", "pass", sigma.checked, sigma.population, "existing sigma contract", sigma.detail)
        )
    except (RuntimeError, ValueError) as exc:
        gates.append(GateResult("PG-1", "fail", 0, 1, "existing sigma contract", str(exc)))

    row_status = "pass" if expected_rows > 0 and actual_rows == expected_rows else "fail"
    gates.append(
        GateResult(
            "PG-2",
            row_status,
            actual_rows,
            expected_rows,
            "exact COUNT(*)",
            f"manifest_rows={expected_rows} reflected_rows={actual_rows}",
        )
    )

    before = {item.table: item for item in untouched_before.tables}
    after = {item.table: item for item in untouched_after.tables}
    unchanged = before == after and bool(before)
    gates.append(
        GateResult(
            "PG-3",
            "pass" if unchanged else "fail",
            len(after),
            len(before),
            "exact COUNT(*) and sample sha256",
            "untouched sources unchanged" if unchanged else f"before={before!r} after={after!r}",
        )
    )

    status = "pass" if all(gate.status == "pass" for gate in gates) else "fail"
    report = PostGateReport(
        run_id=run_id,
        epoch=epoch,
        category=category,
        status=status,
        duration_ms=round((time.monotonic() - started) * 1000, 3),
        rollback_command="python -m pipeline.scripts.rollback --to latest-good --dry-run",
        gates=tuple(gates),
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(asdict(report), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    if status != "pass":
        raise PostGateError(report, report_path)
    return report
