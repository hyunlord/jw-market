"""Pure seven-gate contract for a completed runtime-owned mart reload."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
import json
import math
from typing import Any, Final

try:
    from .post_reload_fdm_contract import ReloadIdentity, validate_evidence as validate_fdm_evidence
    from .post_reload_fdm_values import rows, series
    from .post_reload_mart_common import census_gate as _gate
except ImportError:
    from post_reload_fdm_contract import ReloadIdentity, validate_evidence as validate_fdm_evidence
    from post_reload_fdm_values import rows, series
    from post_reload_mart_common import census_gate as _gate


ABS_TOLERANCE: Final = 0.01
EXPECTED_SOURCE_TABLES: Final = {
    "general_brand": "mart_general_brand_metric",
    "general_market": "mart_general_market_metric",
    "general_dimension": "mart_general_filter_dimension_metric",
    "strategic_brand": "mart_strategic_ml_brand_metric",
    "strategic_market": "mart_strategic_ml_market_metric",
}


def _utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip().replace(" ", "T")
        if not text:
            return None
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _json_object(value: Any) -> Mapping[str, Any]:
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value if isinstance(value, Mapping) else {}


def _source_table_gate(
    evidence: Mapping[str, Any],
    identity: ReloadIdentity,
) -> dict[str, Any]:
    by_logical: dict[str, Mapping[str, Any]] = {}
    failures: list[str] = []
    for row in rows(evidence.get("source_tables")):
        logical_name = str(row.get("logical_name") or "")
        if not logical_name or logical_name in by_logical:
            failures.append(f"source_table_identity_duplicate:{logical_name or '<empty>'}")
            continue
        by_logical[logical_name] = row
    expected_names = set(EXPECTED_SOURCE_TABLES)
    actual_names = set(by_logical)
    if actual_names != expected_names:
        failures.append(
            "source_table_coverage_mismatch:"
            f"missing={sorted(expected_names - actual_names)}:"
            f"extra={sorted(actual_names - expected_names)}"
        )
    checked = 0
    for logical_name, table_name in EXPECTED_SOURCE_TABLES.items():
        row = by_logical.get(logical_name)
        if row is None:
            continue
        row_failures: list[str] = []
        if str(row.get("table_name") or "") != table_name:
            row_failures.append(f"source_table_name_mismatch:{logical_name}")
        if str(row.get("table_schema") or "") != identity.database:
            row_failures.append(f"source_table_schema_mismatch:{logical_name}")
        if int(row.get("row_count") or 0) <= 0:
            row_failures.append(f"source_table_population_empty:{logical_name}")
        computed_min = _utc(row.get("computed_at_min"))
        computed_max = _utc(row.get("computed_at_max"))
        if computed_min is None or computed_max is None:
            row_failures.append(f"source_table_timestamp_missing:{logical_name}")
        elif computed_min > computed_max:
            row_failures.append(f"source_table_timestamp_inverted:{logical_name}")
        if logical_name == "general_dimension":
            expected_marker = _utc(identity.fdm_computed_at)
            if computed_max != expected_marker:
                row_failures.append(
                    "general_dimension_marker_mismatch:"
                    f"actual={row.get('computed_at_max')}:expected={identity.fdm_computed_at}"
                )
        failures.extend(row_failures)
        checked += int(not row_failures)
    return _gate(
        "source_table_freshness",
        checked=checked,
        population=len(EXPECTED_SOURCE_TABLES),
        failures=failures,
        tolerance="exact",
    )


def summarize_specialty_rows(
    evidence_rows: Iterable[Mapping[str, Any]],
    *,
    sparse_periods_are_zero: bool = False,
) -> dict[str, Any]:
    failures: list[str] = []
    checked = 0
    population = 0
    for row in evidence_rows:
        identity = f"{row.get('market_id')}:{row.get('brand_name')}"
        metric_history = series(row.get("metric_history"))
        if not metric_history:
            failures.append(f"specialty_metric_missing:{identity}")
            continue
        population += len(metric_history)
        specialty_data = _json_object(row.get("specialty_data"))
        for period, expected in metric_history.items():
            if expected is None:
                failures.append(f"specialty_metric_invalid:{identity}:{period}")
                continue
            values: list[float] = []
            for history in specialty_data.values():
                parsed = series(history)
                if period not in parsed:
                    if sparse_periods_are_zero:
                        values.append(0.0)
                        continue
                    values = []
                    break
                value = parsed[period]
                if value is None:
                    values = []
                    break
                values.append(value)
            if not values:
                failures.append(f"specialty_coverage_missing:{identity}:{period}")
                continue
            checked += 1
            actual = sum(values)
            if not math.isclose(actual, expected, rel_tol=0.0, abs_tol=ABS_TOLERANCE):
                failures.append(
                    f"specialty_total_mismatch:{identity}:{period}:"
                    f"actual={actual}:expected={expected}"
                )
    return {
        "checked": checked,
        "population": population,
        "failures": failures,
    }


def _specialty_gate(
    evidence_rows: Any,
    gate_name: str,
) -> dict[str, Any]:
    is_summary = isinstance(evidence_rows, Mapping) and {
        "checked",
        "population",
        "failures",
    }.issubset(evidence_rows)
    summary = (
        evidence_rows
        if is_summary
        else summarize_specialty_rows(
            rows(evidence_rows),
            sparse_periods_are_zero=gate_name == "strategic_specialty_parity",
        )
    )
    failures = [str(failure) for failure in summary.get("failures") or []]
    return _gate(
        gate_name,
        checked=int(summary.get("checked") or 0),
        population=int(summary.get("population") or 0),
        failures=failures,
        tolerance=f"absolute:{ABS_TOLERANCE}",
    )


def validate_evidence(
    evidence: Mapping[str, Any],
    identity: ReloadIdentity,
) -> dict[str, Any]:
    """Validate current mart sources without depending on retired projection tables."""

    fdm_report = validate_fdm_evidence(evidence, identity)
    fdm_gates = fdm_report["gates"]
    gates = [
        fdm_gates[0],
        fdm_gates[1],
        _source_table_gate(evidence, identity),
        fdm_gates[2],
        fdm_gates[3],
        _specialty_gate(
            evidence.get("general_specialty_summary", evidence.get("general_specialty_rows")),
            "general_specialty_parity",
        ),
        _specialty_gate(
            evidence.get("strategic_specialty_summary", evidence.get("strategic_specialty_rows")),
            "strategic_specialty_parity",
        ),
    ]
    return {
        "reload_authorization": fdm_report["reload_authorization"],
        "gates": gates,
        "exit_code": int(any(gate["exit_code"] for gate in gates)),
    }
