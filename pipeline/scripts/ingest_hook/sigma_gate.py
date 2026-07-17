"""Σ(parts) = whole gate over a staging table.

The rehearsal loader lands submissions in a staging table with a ``level``
column where one marker row per period carries the whole ("전체") and every
other row is a part. The gate reconciles every period; one correct period
cannot hide a broken one. Production categories pin their own sigma SQL at
activation (PL gate) — the mechanism and failure shape are identical.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

TOTAL_MARKER = "전체"
ABS_TOL = 0.01


class SigmaGateError(ValueError):
    pass


@dataclass
class SigmaReport:
    table: str
    periods: dict[str, tuple[float, float]] = field(default_factory=dict)  # period -> (parts_sum, whole)


def check_staging(
    conn,
    table: str,
    *,
    period_col: str = "period",
    level_col: str = "level",
    value_col: str = "value",
    total_marker: str = TOTAL_MARKER,
    abs_tol: float = ABS_TOL,
) -> SigmaReport:
    report = SigmaReport(table=table)
    cursor = conn.cursor()
    cursor.execute(f"SELECT DISTINCT {period_col} FROM {table}")  # noqa: S608 - table from internal config
    periods = [row[0] for row in cursor.fetchall()]
    if not periods:
        raise SigmaGateError(f"{table}: staging table is empty; nothing to reconcile")

    failures: list[str] = []
    for period in sorted(periods):
        cursor.execute(
            f"SELECT SUM(CASE WHEN {level_col} = ? THEN {value_col} ELSE 0 END),"
            f"       SUM(CASE WHEN {level_col} <> ? THEN {value_col} ELSE 0 END),"
            f"       SUM(CASE WHEN {level_col} = ? THEN 1 ELSE 0 END)"
            f" FROM {table} WHERE {period_col} = ?",  # noqa: S608
            (total_marker, total_marker, total_marker, period),
        )
        whole, parts_sum, marker_count = cursor.fetchone()
        if not marker_count:
            failures.append(f"{period}: no {total_marker!r} whole row")
            continue
        whole = float(whole or 0.0)
        parts_sum = float(parts_sum or 0.0)
        if not (math.isfinite(whole) and math.isfinite(parts_sum)):
            failures.append(f"{period}: non-finite values (whole={whole}, parts={parts_sum})")
            continue
        if abs(whole - parts_sum) > abs_tol:
            failures.append(f"{period}: Σparts {parts_sum} != whole {whole} (tol {abs_tol})")
        report.periods[str(period)] = (parts_sum, whole)

    if failures:
        raise SigmaGateError("; ".join(failures))
    return report
