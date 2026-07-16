from __future__ import annotations

from copy import deepcopy
import importlib.util
import inspect
from pathlib import Path
import re
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT_ROOT / "scripts" / "post_reload_mart_gate.py"
MODULE_SPEC = importlib.util.spec_from_file_location("post_reload_mart_gate_under_test", MODULE_PATH)
assert MODULE_SPEC is not None
assert MODULE_SPEC.loader is not None
sys.path.insert(0, str(MODULE_PATH.parent))
post_reload_mart_gate = importlib.util.module_from_spec(MODULE_SPEC)
sys.modules[MODULE_SPEC.name] = post_reload_mart_gate
MODULE_SPEC.loader.exec_module(post_reload_mart_gate)

MARKER = "2026-07-16T08:26:03Z"
DATABASE = "jw_mart_d2_stage_20260630_r2"
DIMENSIONS = (
    "seller",
    "molecule_strength",
    "form",
    "route",
    "reimbursement",
)
EXPECTED_GATES = [
    "mart_reload_identity",
    "fdm_reload_cohort",
    "source_table_freshness",
    "general_dimension_parity",
    "molecule_parity",
    "general_specialty_parity",
    "strategic_specialty_parity",
]


def _history(april: float, may: float) -> dict[str, dict[str, float]]:
    return {
        "2026-04": {"raw_value": april},
        "2026-05": {"raw_value": may},
    }


def _identity() -> post_reload_mart_gate.ReloadIdentity:
    return post_reload_mart_gate.ReloadIdentity(
        reload_run_id="dimfix5_20260716",
        database=DATABASE,
        fdm_computed_at=MARKER,
    )


def _source_tables() -> list[dict[str, object]]:
    tables = (
        ("general_brand", "mart_general_brand_metric"),
        ("general_market", "mart_general_market_metric"),
        ("general_dimension", "mart_general_filter_dimension_metric"),
        ("strategic_brand", "mart_strategic_ml_brand_metric"),
        ("strategic_market", "mart_strategic_ml_market_metric"),
    )
    return [
        {
            "logical_name": logical_name,
            "table_schema": DATABASE,
            "table_name": table_name,
            "row_count": 10,
            "computed_at_min": MARKER,
            "computed_at_max": MARKER,
        }
        for logical_name, table_name in tables
    ]


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
    specialty = {
        "내과": _history(36.0, 40.0),
        "순환기": _history(54.0, 60.0),
    }
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
        "source_tables": _source_tables(),
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
        "general_specialty_rows": [
            {
                "market_id": "C10A1",
                "brand_name": "Brand A",
                "metric_history": _history(90.0, 100.0),
                "specialty_data": specialty,
            }
        ],
        "strategic_specialty_rows": [
            {
                "market_id": "ml_001",
                "brand_name": "Brand A",
                "metric_history": _history(90.0, 100.0),
                "specialty_data": specialty,
            }
        ],
    }


def _gate(report: dict[str, object], name: str) -> dict[str, object]:
    gates = report["gates"]
    assert isinstance(gates, list)
    return next(gate for gate in gates if gate["gate"] == name)


def test_valid_runtime_owned_snapshot_passes_exactly_seven_census_gates() -> None:
    report = post_reload_mart_gate.validate_evidence(_valid_evidence(), _identity())

    assert report["exit_code"] == 0
    assert [gate["gate"] for gate in report["gates"]] == EXPECTED_GATES
    assert all(gate["checked"] == gate["population"] for gate in report["gates"])
    assert all(gate["population"] > 0 for gate in report["gates"])


def test_zero_market_population_fails_instead_of_vacuously_passing() -> None:
    evidence = _valid_evidence()
    evidence["market_rows"] = []

    report = post_reload_mart_gate.validate_evidence(evidence, _identity())

    gate = _gate(report, "general_dimension_parity")
    assert report["exit_code"] == 1
    assert gate["population"] == 0
    assert gate["exit_code"] == 1


def test_mixed_fdm_marker_fails_closed() -> None:
    evidence = _valid_evidence()
    evidence["fdm_marker_rows"][0]["marker_count"] = 2  # type: ignore[index]
    evidence["fdm_marker_rows"][0]["computed_at_min"] = "2026-07-15T00:00:00Z"  # type: ignore[index]

    report = post_reload_mart_gate.validate_evidence(evidence, _identity())

    gate = _gate(report, "fdm_reload_cohort")
    assert report["exit_code"] == 1
    assert gate["exit_code"] == 1
    assert any("marker" in failure for failure in gate["failure_reasons"])


def test_general_dimension_drift_fails_without_fixed_market_total() -> None:
    evidence = _valid_evidence()
    evidence["sidecar_rows"][0]["raw_value_history"] = _history(46.0, 50.0)  # type: ignore[index]

    report = post_reload_mart_gate.validate_evidence(evidence, _identity())

    gate = _gate(report, "general_dimension_parity")
    assert gate["exit_code"] == 1
    assert any("dimension_total_mismatch" in failure for failure in gate["failure_reasons"])


def test_molecule_drift_fails_independently_from_promoted_sidecars() -> None:
    evidence = _valid_evidence()
    evidence["molecule_rows"][0]["raw_value_history"] = _history(46.0, 50.0)  # type: ignore[index]

    report = post_reload_mart_gate.validate_evidence(evidence, _identity())

    assert _gate(report, "general_dimension_parity")["exit_code"] == 0
    molecule = _gate(report, "molecule_parity")
    assert molecule["exit_code"] == 1
    assert any("dimension_total_mismatch" in failure for failure in molecule["failure_reasons"])


def test_source_census_fails_when_table_is_empty_or_missing() -> None:
    evidence = _valid_evidence()
    evidence["source_tables"] = evidence["source_tables"][:-1]  # type: ignore[index]
    evidence["source_tables"][0]["row_count"] = 0  # type: ignore[index]

    report = post_reload_mart_gate.validate_evidence(evidence, _identity())

    gate = _gate(report, "source_table_freshness")
    assert gate["exit_code"] == 1
    assert any("coverage_mismatch" in failure for failure in gate["failure_reasons"])
    assert any("population_empty" in failure for failure in gate["failure_reasons"])


def test_source_census_fails_when_timestamp_is_missing_or_inverted() -> None:
    evidence = _valid_evidence()
    evidence["source_tables"][0]["computed_at_min"] = None  # type: ignore[index]
    evidence["source_tables"][1]["computed_at_min"] = "2026-07-16T09:00:00Z"  # type: ignore[index]
    evidence["source_tables"][1]["computed_at_max"] = "2026-07-16T08:00:00Z"  # type: ignore[index]

    report = post_reload_mart_gate.validate_evidence(evidence, _identity())

    gate = _gate(report, "source_table_freshness")
    assert gate["exit_code"] == 1
    assert any("timestamp_missing" in failure for failure in gate["failure_reasons"])
    assert any("timestamp_inverted" in failure for failure in gate["failure_reasons"])


def test_general_dimension_source_must_end_at_authorized_marker() -> None:
    evidence = _valid_evidence()
    general_dimension = next(
        row
        for row in evidence["source_tables"]  # type: ignore[union-attr]
        if row["logical_name"] == "general_dimension"
    )
    general_dimension["computed_at_max"] = "2026-07-16T08:25:59Z"

    report = post_reload_mart_gate.validate_evidence(evidence, _identity())

    gate = _gate(report, "source_table_freshness")
    assert gate["exit_code"] == 1
    assert any("general_dimension_marker_mismatch" in failure for failure in gate["failure_reasons"])


def test_general_and_strategic_specialty_drift_fail_independently() -> None:
    general = _valid_evidence()
    general["general_specialty_rows"][0]["specialty_data"]["내과"] = _history(37.0, 41.0)  # type: ignore[index]
    strategic = _valid_evidence()
    strategic["strategic_specialty_rows"] = []

    general_report = post_reload_mart_gate.validate_evidence(general, _identity())
    strategic_report = post_reload_mart_gate.validate_evidence(strategic, _identity())

    assert _gate(general_report, "general_specialty_parity")["exit_code"] == 1
    strategic_gate = _gate(strategic_report, "strategic_specialty_parity")
    assert strategic_gate["population"] == 0
    assert strategic_gate["exit_code"] == 1


def test_wrong_database_or_marker_is_rejected_even_when_values_match() -> None:
    wrong_database = deepcopy(_valid_evidence())
    wrong_database["database"] = "jw_mart"
    wrong_marker = post_reload_mart_gate.ReloadIdentity(
        reload_run_id="dimfix5_20260716",
        database=DATABASE,
        fdm_computed_at="2026-07-16T08:25:59Z",
    )

    database_report = post_reload_mart_gate.validate_evidence(wrong_database, _identity())
    marker_report = post_reload_mart_gate.validate_evidence(_valid_evidence(), wrong_marker)

    assert _gate(database_report, "mart_reload_identity")["exit_code"] == 1
    assert _gate(marker_report, "fdm_reload_cohort")["exit_code"] == 1


def test_runtime_collector_is_read_only_and_has_no_legacy_block_dependency() -> None:
    source = inspect.getsource(post_reload_mart_gate.collect_runtime_evidence)
    module_source = inspect.getsource(post_reload_mart_gate)
    normalized = re.sub(r"[_,.\s]", "", module_source)

    assert "SET SESSION TRANSACTION READ ONLY" in source
    assert "START TRANSACTION READ ONLY" in source
    assert "connection.rollback()" in source
    assert "mart_analysis_level_block" not in module_source
    assert "source_epoch" not in module_source
    assert "build_version" not in module_source
    assert not re.search(r"\b(?:INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER|TRUNCATE)\b", source)
    assert "127504" not in normalized
    assert "82054" not in normalized
