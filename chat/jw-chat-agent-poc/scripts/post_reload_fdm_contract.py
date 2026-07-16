"""Pure acceptance contract for a completed general-view FDM reload."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
import math
from typing import Any, Final

try:
    from .post_reload_fdm_values import rows as _rows
    from .post_reload_fdm_values import series as _series
except ImportError:
    from post_reload_fdm_values import rows as _rows
    from post_reload_fdm_values import series as _series


ABS_TOLERANCE: Final = 0.01
SIDECAR_DIMENSIONS: Final = (
    "seller",
    "molecule_strength",
    "form",
    "route",
    "reimbursement",
)


@dataclass(frozen=True, slots=True)
class ReloadIdentity:
    """PL-authorized identity for one promoted FDM cohort."""

    reload_run_id: str
    database: str
    fdm_computed_at: str


def _gate(
    name: str,
    *,
    checked: int,
    population: int,
    failures: Sequence[str],
    tolerance: str,
) -> dict[str, Any]:
    failure_list = list(failures)
    exit_code = int(population == 0 or checked != population or bool(failure_list))
    return {
        "gate": name,
        "classification": "census",
        "checked": checked,
        "population": population,
        "missing": "fail",
        "tolerance": tolerance,
        "failures": failure_list,
        "failure_reasons": failure_list,
        "failure_count": len(failure_list),
        "exit_code": exit_code,
        "environment": "runtime_mart_read_only",
    }


def _market_totals(rows: Iterable[Mapping[str, Any]]) -> tuple[dict[str, dict[str, float]], list[str]]:
    totals: dict[str, dict[str, float]] = {}
    failures: list[str] = []
    for row in rows:
        market = str(row.get("market_id") or "").strip()
        parsed_series = _series(row.get("market_size_series"))
        for period, amount in parsed_series.items():
            if amount is None:
                failures.append(f"market_period_invalid:{market or '<empty>'}:{period}")
        series = {
            period: amount
            for period, amount in parsed_series.items()
            if amount is not None
        }
        if not market or not series:
            failures.append(f"market_headline_missing:{market or '<empty>'}")
        elif market in totals:
            failures.append(f"market_headline_duplicate:{market}")
        else:
            totals[market] = series
    return totals, failures


def _identity_gate(evidence: Mapping[str, Any], identity: ReloadIdentity) -> dict[str, Any]:
    actual = str(evidence.get("database") or "").strip()
    failures = (
        []
        if actual == identity.database
        else [f"database_mismatch:actual={actual or '<missing>'}:expected={identity.database}"]
    )
    return _gate(
        "mart_reload_identity",
        checked=int(not failures),
        population=1,
        failures=failures,
        tolerance="exact",
    )


def _cohort_gate(evidence: Mapping[str, Any], identity: ReloadIdentity) -> dict[str, Any]:
    rows = _rows(evidence.get("fdm_marker_rows"))
    sidecar_rows = _rows(evidence.get("sidecar_rows"))
    by_dimension: dict[str, Mapping[str, Any]] = {}
    captured_by_dimension: dict[str, int] = defaultdict(int)
    failures: list[str] = []
    if int(evidence.get("tx_read_only") or 0) != 1:
        failures.append("transaction_is_not_read_only")
    for row in rows:
        dimension = str(row.get("dimension_type") or "")
        if not dimension or dimension in by_dimension:
            failures.append(f"dimension_marker_duplicate:{dimension or '<empty>'}")
        else:
            by_dimension[dimension] = row
    expected = set(SIDECAR_DIMENSIONS)
    actual = set(by_dimension)
    for row in sidecar_rows:
        captured_by_dimension[str(row.get("dimension_type") or "")] += 1
    if actual != expected:
        failures.append(
            f"dimension_coverage_mismatch:missing={sorted(expected - actual)}:extra={sorted(actual - expected)}"
        )
    checked = 0
    reported_rows = 0
    for dimension in SIDECAR_DIMENSIONS:
        row = by_dimension.get(dimension)
        if row is None:
            continue
        row_count = int(row.get("row_count") or 0)
        marker_count = int(row.get("marker_count") or 0)
        marker_min = str(row.get("computed_at_min") or "")
        marker_max = str(row.get("computed_at_max") or "")
        reported_rows += row_count
        captured_rows = captured_by_dimension.get(dimension, 0)
        marker_matches = marker_min == identity.fdm_computed_at and marker_max == identity.fdm_computed_at
        if row_count <= 0:
            failures.append(f"dimension_population_empty:{dimension}")
        if row_count != captured_rows:
            failures.append(
                f"fdm_dimension_row_count_mismatch:{dimension}:"
                f"reported={row_count}:captured={captured_rows}"
            )
        if marker_count != 1:
            failures.append(f"marker_count_mismatch:{dimension}:actual={marker_count}:expected=1")
        if not marker_matches:
            failures.append(
                f"marker_mismatch:{dimension}:actual={marker_min}..{marker_max}:expected={identity.fdm_computed_at}"
            )
        if row_count > 0 and marker_count == 1 and marker_matches:
            checked += 1
    captured_rows = len(sidecar_rows)
    if reported_rows != captured_rows:
        failures.append(f"fdm_row_count_mismatch:reported={reported_rows}:captured={captured_rows}")
    return _gate(
        "fdm_reload_cohort",
        checked=checked,
        population=len(SIDECAR_DIMENSIONS),
        failures=failures,
        tolerance="exact",
    )


def _parity_gate(
    evidence: Mapping[str, Any],
    *,
    row_key: str,
    dimensions: Sequence[str],
    gate_name: str,
) -> dict[str, Any]:
    markets, failures = _market_totals(_rows(evidence.get("market_rows")))
    grouped: dict[tuple[str, str], list[dict[str, float | None]]] = defaultdict(list)
    for row in _rows(evidence.get(row_key)):
        grouped[
            (
                str(row.get("market_id") or "").strip(),
                str(row.get("dimension_type") or "").strip(),
            )
        ].append(_series(row.get("raw_value_history")))
    expected_markets = set(markets)
    expected_dimensions = set(dimensions)
    for market, dimension in grouped:
        if market not in expected_markets:
            failures.append(f"orphan_dimension_market:{market or '<empty>'}:{dimension or '<empty>'}")
        if dimension not in expected_dimensions:
            failures.append(
                f"unexpected_dimension_type:{market or '<empty>'}:{dimension or '<empty>'}"
            )
    population = sum(len(periods) * len(dimensions) for periods in markets.values())
    checked = 0
    for market, periods in markets.items():
        for dimension in dimensions:
            histories = grouped.get((market, dimension), [])
            if not histories:
                failures.append(f"dimension_population_missing:{market}:{dimension}")
                continue
            for period, expected in periods.items():
                values: list[float] = []
                malformed = False
                for history in histories:
                    if period not in history:
                        values.append(0.0)
                        continue
                    value = history[period]
                    if value is None:
                        malformed = True
                        break
                    values.append(value)
                if malformed:
                    failures.append(f"dimension_period_invalid:{market}:{dimension}:{period}")
                    continue
                checked += 1
                actual = sum(values)
                if not math.isclose(actual, expected, rel_tol=0.0, abs_tol=ABS_TOLERANCE):
                    failures.append(
                        f"dimension_total_mismatch:{market}:{dimension}:{period}:"
                        f"actual={actual}:expected={expected}"
                    )
    return _gate(
        gate_name,
        checked=checked,
        population=population,
        failures=failures,
        tolerance=f"absolute:{ABS_TOLERANCE}",
    )


def validate_evidence(
    evidence: Mapping[str, Any],
    identity: ReloadIdentity,
) -> dict[str, Any]:
    """Validate promoted FDM and untouched molecule rows against mart headlines."""

    gates = [
        _identity_gate(evidence, identity),
        _cohort_gate(evidence, identity),
        _parity_gate(
            evidence,
            row_key="sidecar_rows",
            dimensions=SIDECAR_DIMENSIONS,
            gate_name="general_dimension_parity",
        ),
        _parity_gate(
            evidence,
            row_key="molecule_rows",
            dimensions=("molecule",),
            gate_name="molecule_parity",
        ),
    ]
    return {
        "reload_authorization": {
            "reload_run_id": identity.reload_run_id,
            "database": identity.database,
            "fdm_computed_at": identity.fdm_computed_at,
        },
        "gates": gates,
        "exit_code": int(any(gate["exit_code"] for gate in gates)),
    }
