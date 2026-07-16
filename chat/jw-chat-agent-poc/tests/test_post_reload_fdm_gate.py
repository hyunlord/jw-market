from __future__ import annotations

from copy import deepcopy
import importlib.util
import inspect
from pathlib import Path
import re
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT_ROOT / "scripts" / "post_reload_fdm_gate.py"
MODULE_SPEC = importlib.util.spec_from_file_location("post_reload_fdm_gate_under_test", MODULE_PATH)
assert MODULE_SPEC is not None
assert MODULE_SPEC.loader is not None
sys.path.insert(0, str(MODULE_PATH.parent))
post_reload_fdm_gate = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = post_reload_fdm_gate
MODULE_SPEC.loader.exec_module(post_reload_fdm_gate)


DIMENSIONS = (
    "seller",
    "molecule_strength",
    "form",
    "route",
    "reimbursement",
)
MARKER = "2026-07-16T08:26:03Z"
DATABASE = "jw_mart_d2_stage_20260630_r2"


def _history(first: float, second: float) -> dict[str, dict[str, float]]:
    return {
        "2026-04": {"raw_value": first},
        "2026-05": {"raw_value": second},
    }


def _identity() -> post_reload_fdm_gate.ReloadIdentity:
    return post_reload_fdm_gate.ReloadIdentity(
        reload_run_id="dimfix5_20260716",
        database=DATABASE,
        fdm_computed_at=MARKER,
    )


def _valid_evidence() -> dict[str, object]:
    sidecar_rows = [
        {
            "market_id": "C10A1",
            "dimension_type": dimension,
            "raw_value_history": _history(april, may),
        }
        for dimension in DIMENSIONS
        for april, may in ((36.0, 40.0), (54.0, 60.0))
    ]
    return {
        "tx_read_only": 1,
        "database": DATABASE,
        "fdm_marker_rows": [
            {
                "dimension_type": dimension,
                "row_count": 2,
                "marker_count": 1,
                "computed_at_min": MARKER,
                "computed_at_max": MARKER,
            }
            for dimension in DIMENSIONS
        ],
        "market_rows": [
            {
                "market_id": "C10A1",
                "market_size_series": _history(90.0, 100.0),
            }
        ],
        "sidecar_rows": sidecar_rows,
        "molecule_rows": [
            {
                "market_id": "C10A1",
                "dimension_type": "molecule",
                "raw_value_history": _history(45.0, 50.0),
            },
            {
                "market_id": "C10A1",
                "dimension_type": "molecule",
                "raw_value_history": _history(45.0, 50.0),
            },
        ],
    }


def _gate(report: dict[str, object], name: str) -> dict[str, object]:
    gates = report["gates"]
    assert isinstance(gates, list)
    return next(gate for gate in gates if gate["gate"] == name)


def test_valid_fdm_reload_passes_every_census_gate() -> None:
    report = post_reload_fdm_gate.validate_evidence(_valid_evidence(), _identity())

    assert report["exit_code"] == 0
    assert [gate["gate"] for gate in report["gates"]] == [
        "mart_reload_identity",
        "fdm_reload_cohort",
        "general_dimension_parity",
        "molecule_parity",
    ]
    assert all(gate["checked"] == gate["population"] for gate in report["gates"])


def test_mixed_or_wrong_fdm_marker_fails_closed() -> None:
    evidence = _valid_evidence()
    evidence["fdm_marker_rows"][0]["marker_count"] = 2  # type: ignore[index]
    evidence["fdm_marker_rows"][0]["computed_at_min"] = "2026-06-29T00:00:00Z"  # type: ignore[index]

    report = post_reload_fdm_gate.validate_evidence(evidence, _identity())

    gate = _gate(report, "fdm_reload_cohort")
    assert report["exit_code"] == 1
    assert gate["exit_code"] == 1
    assert any("marker" in failure for failure in gate["failure_reasons"])


def test_missing_dimension_and_zero_population_do_not_vacuously_pass() -> None:
    evidence = _valid_evidence()
    evidence["fdm_marker_rows"] = []  # type: ignore[index]
    evidence["sidecar_rows"] = []  # type: ignore[index]
    evidence["market_rows"] = []  # type: ignore[index]

    report = post_reload_fdm_gate.validate_evidence(evidence, _identity())

    assert report["exit_code"] == 1
    assert _gate(report, "fdm_reload_cohort")["exit_code"] == 1
    parity = _gate(report, "general_dimension_parity")
    assert parity["population"] == 0
    assert parity["exit_code"] == 1


def test_inflated_sidecar_total_fails_without_fixed_market_golden() -> None:
    evidence = _valid_evidence()
    evidence["sidecar_rows"][0]["raw_value_history"] = _history(46.0, 50.0)  # type: ignore[index]

    report = post_reload_fdm_gate.validate_evidence(evidence, _identity())

    gate = _gate(report, "general_dimension_parity")
    assert gate["exit_code"] == 1
    assert any("dimension_total_mismatch" in failure for failure in gate["failure_reasons"])


def test_missing_positive_only_history_period_is_counted_as_zero() -> None:
    evidence = _valid_evidence()
    for row in evidence["sidecar_rows"]:  # type: ignore[index]
        if row["dimension_type"] == "seller":
            row["raw_value_history"] = {  # type: ignore[index]
                "2026-05": row["raw_value_history"]["2026-05"],  # type: ignore[index]
            }
    seller_rows = [
        row
        for row in evidence["sidecar_rows"]  # type: ignore[index]
        if row["dimension_type"] == "seller"
    ]
    seller_rows[0]["raw_value_history"]["2026-04"] = {"raw_value": 90.0}

    report = post_reload_fdm_gate.validate_evidence(evidence, _identity())

    gate = _gate(report, "general_dimension_parity")
    assert gate["exit_code"] == 0
    assert gate["checked"] == gate["population"]


def test_explicit_unparseable_history_value_still_fails_closed() -> None:
    evidence = _valid_evidence()
    evidence["sidecar_rows"][0]["raw_value_history"]["2026-04"] = {"raw_value": None}  # type: ignore[index]

    report = post_reload_fdm_gate.validate_evidence(evidence, _identity())

    gate = _gate(report, "general_dimension_parity")
    assert gate["exit_code"] == 1
    assert any("dimension_period_invalid" in failure for failure in gate["failure_reasons"])


def test_molecule_truth_basis_is_checked_without_requiring_reload_marker() -> None:
    evidence = _valid_evidence()
    evidence["molecule_rows"][0]["raw_value_history"] = _history(46.0, 50.0)  # type: ignore[index]

    report = post_reload_fdm_gate.validate_evidence(evidence, _identity())

    assert _gate(report, "general_dimension_parity")["exit_code"] == 0
    molecule = _gate(report, "molecule_parity")
    assert molecule["exit_code"] == 1
    assert any("dimension_total_mismatch" in failure for failure in molecule["failure_reasons"])


def test_wrong_database_is_rejected_even_when_values_match() -> None:
    evidence = deepcopy(_valid_evidence())
    evidence["database"] = "jw_mart"

    report = post_reload_fdm_gate.validate_evidence(evidence, _identity())

    identity = _gate(report, "mart_reload_identity")
    assert identity["exit_code"] == 1
    assert any("database_mismatch" in failure for failure in identity["failure_reasons"])


def test_partially_unparseable_market_history_fails_closed() -> None:
    evidence = _valid_evidence()
    evidence["market_rows"][0]["market_size_series"]["2026-04"] = {"raw_value": None}  # type: ignore[index]

    report = post_reload_fdm_gate.validate_evidence(evidence, _identity())

    parity = _gate(report, "general_dimension_parity")
    assert report["exit_code"] == 1
    assert parity["exit_code"] == 1
    assert any("market_period_invalid:C10A1:2026-04" in failure for failure in parity["failure_reasons"])


def test_orphan_sidecar_market_fails_instead_of_being_ignored() -> None:
    evidence = _valid_evidence()
    evidence["sidecar_rows"].append(  # type: ignore[union-attr]
        {
            "market_id": "ORPHAN",
            "dimension_type": "seller",
            "raw_value_history": _history(1.0, 1.0),
        }
    )
    seller_marker = next(
        row
        for row in evidence["fdm_marker_rows"]  # type: ignore[index]
        if row["dimension_type"] == "seller"
    )
    seller_marker["row_count"] = 3

    report = post_reload_fdm_gate.validate_evidence(evidence, _identity())

    parity = _gate(report, "general_dimension_parity")
    assert report["exit_code"] == 1
    assert parity["exit_code"] == 1
    assert any("orphan_dimension_market:ORPHAN:seller" in failure for failure in parity["failure_reasons"])


def test_marker_row_count_is_checked_per_dimension() -> None:
    evidence = _valid_evidence()
    seller_marker = next(
        row
        for row in evidence["fdm_marker_rows"]  # type: ignore[index]
        if row["dimension_type"] == "seller"
    )
    route_marker = next(
        row
        for row in evidence["fdm_marker_rows"]  # type: ignore[index]
        if row["dimension_type"] == "route"
    )
    seller_marker["row_count"] = 3
    route_marker["row_count"] = 1

    report = post_reload_fdm_gate.validate_evidence(evidence, _identity())

    cohort = _gate(report, "fdm_reload_cohort")
    assert report["exit_code"] == 1
    assert cohort["exit_code"] == 1
    assert any("fdm_dimension_row_count_mismatch:seller" in failure for failure in cohort["failure_reasons"])
    assert any("fdm_dimension_row_count_mismatch:route" in failure for failure in cohort["failure_reasons"])


def test_runtime_collector_targets_fdm_and_is_read_only() -> None:
    source = inspect.getsource(post_reload_fdm_gate.collect_runtime_evidence)
    normalized = re.sub(r"[_,.\s]", "", inspect.getsource(post_reload_fdm_gate))

    assert "SET SESSION TRANSACTION READ ONLY" in source
    assert "START TRANSACTION READ ONLY" in source
    assert "connection.rollback()" in source
    assert "mart_general_filter_dimension_metric" in source
    assert "computed_at = %s" in source
    assert "mart_analysis_level_block" not in source
    assert "source_epoch" not in source
    assert "build_version" not in source
    assert not re.search(r"\b(?:INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER|TRUNCATE)\b", source)
    assert "127504" not in normalized
    assert "82054" not in normalized
