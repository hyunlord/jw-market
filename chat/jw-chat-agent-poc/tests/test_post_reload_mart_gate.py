from __future__ import annotations

from copy import deepcopy
import inspect
import json
import os
from pathlib import Path
import re
import subprocess
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import post_reload_mart_gate  # noqa: E402
from scripts.post_reload_mart_gate import validate_evidence  # noqa: E402


SIDECAR_DIMENSIONS = (
    "seller",
    "molecule_strength",
    "form",
    "route",
    "reimbursement",
)

LEVELS = (
    "판매사",
    "성분",
    "성분용량",
    "제형",
    "투여경로",
    "급여구분",
)

GATE_SCRIPT = PROJECT_ROOT / "scripts" / "post_reload_mart_gate.py"


def _history(value: float) -> dict[str, dict[str, float]]:
    return {"2026-05": {"raw_value": value}}


def _history_periods(april: float, may: float) -> dict[str, dict[str, float]]:
    return {
        "2026-04": {"raw_value": april},
        "2026-05": {"raw_value": may},
    }


def _monthly_periods(count: int) -> list[str]:
    periods: list[str] = []
    year, month = 2021, 5
    for _ in range(count):
        periods.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year += 1
            month = 1
    return periods


def _constant_history(periods: list[str], value: float) -> dict[str, dict[str, float]]:
    return {period: {"raw_value": value} for period in periods}


def _constant_segments(period_count: int) -> list[dict[str, object]]:
    return [
        {"name": "전체", "is_overall": True, "value_series": [100.0] * period_count},
        {"name": "A", "value_series": [40.0] * period_count},
        {"name": "B", "value_series": [60.0] * period_count},
    ]


def _segments() -> list[dict[str, object]]:
    return [
        {"name": "전체", "is_overall": True, "value_series": [100.0]},
        {"name": "A", "value_series": [40.0]},
        {"name": "B", "value_series": [60.0]},
    ]


def _segments_periods() -> list[dict[str, object]]:
    return [
        {"name": "전체", "is_overall": True, "value_series": [90.0, 100.0]},
        {"name": "A", "value_series": [36.0, 40.0]},
        {"name": "B", "value_series": [54.0, 60.0]},
    ]


def _source_tables() -> list[dict[str, object]]:
    return [
        {
            "logical_name": logical_name,
            "table_schema": "jw_mart",
            "table_name": table_name,
            "row_count": 10,
            "computed_at_min": "2026-07-16T10:00:00Z",
            "computed_at_max": "2026-07-16T10:00:00Z",
        }
        for logical_name, table_name in (
            ("general_brand", "mart_general_brand_metric"),
            ("general_market", "mart_general_market_metric"),
            ("general_dimension", "mart_general_filter_dimension_metric"),
            ("strategic_brand", "mart_strategic_ml_brand_metric"),
            ("strategic_market", "mart_strategic_ml_market_metric"),
        )
    ]


def _valid_evidence() -> dict[str, object]:
    source_tables = _source_tables()
    source_epoch = "opaque-producer-epoch"
    analysis_levels = {
        "levels": list(LEVELS),
        "periods_monthly": ["2026-05"],
        "data": {
            level: {"segments": _segments(), "by_channel": {"전체": _segments()}}
            for level in LEVELS
        },
    }
    return {
        "tx_read_only": 1,
        "cohort": {
            "source_epoch": source_epoch,
            "build_version": "v-test",
            "built_at_min": "2026-07-16T10:01:00Z",
            "built_at_max": "2026-07-16T10:02:00Z",
            "row_count": 1,
        },
        "source_tables": source_tables,
        "market_rows": [
            {
                "market_id": "A01A1",
                "market_size_series": _history(100.0),
            }
        ],
        "dimension_rows": [
            {
                "market_id": "A01A1",
                "dimension_type": dimension,
                "dimension_value": label,
                "raw_value_history": _history(value),
            }
            for dimension in SIDECAR_DIMENSIONS
            for label, value in (("A", 40.0), ("B", 60.0))
        ],
        "block_rows": [
            {
                "market_id": "A01A1",
                "source_epoch": source_epoch,
                "build_version": "v-test",
                "analysis_levels_json": analysis_levels,
            }
        ],
        "general_specialty_rows": [
            {
                "market_id": "A01A1",
                "brand_name": "Brand A",
                "metric_history": _history(100.0),
                "specialty_data": {"내과": _history(40.0), "순환기": _history(60.0)},
            }
        ],
        "strategic_specialty_rows": [
            {
                "market_id": "ml_001",
                "brand_name": "Brand A",
                "metric_history": _history(100.0),
                "specialty_data": {"내과": _history(40.0), "순환기": _history(60.0)},
            }
        ],
    }


def _gate(report: dict[str, object], name: str) -> dict[str, object]:
    return next(item for item in report["gates"] if item["gate"] == name)  # type: ignore[index]


def _run_gate(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GATE_SCRIPT), *args],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _write_evidence(path: Path, evidence: dict[str, object]) -> None:
    path.write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")


def test_valid_snapshot_passes_all_census_gates() -> None:
    report = validate_evidence(_valid_evidence())

    assert report["exit_code"] == 0
    assert all(gate["exit_code"] == 0 for gate in report["gates"])
    assert all(gate["checked"] == gate["population"] for gate in report["gates"])


def test_disabled_molecule_sidecar_is_not_required_but_block_level_is() -> None:
    evidence = _valid_evidence()

    report = validate_evidence(evidence)

    assert _gate(report, "general_dimension_parity")["exit_code"] == 0
    assert _gate(report, "analysis_level_block_parity")["exit_code"] == 0


def test_inflated_dimension_sum_fails() -> None:
    evidence = _valid_evidence()
    evidence["dimension_rows"][0]["raw_value_history"] = _history(50.0)  # type: ignore[index]

    report = validate_evidence(evidence)

    gate = _gate(report, "general_dimension_parity")
    assert report["exit_code"] == 1
    assert gate["exit_code"] == 1
    assert gate["failures"]


def test_compensated_missing_sidecar_option_fails_cross_contract_census() -> None:
    evidence = _valid_evidence()
    evidence["dimension_rows"] = [  # type: ignore[index]
        row
        for row in evidence["dimension_rows"]  # type: ignore[index]
        if not (row["dimension_type"] == "seller" and row["dimension_value"] == "B")
    ]
    seller_a = next(
        row
        for row in evidence["dimension_rows"]  # type: ignore[index]
        if row["dimension_type"] == "seller" and row["dimension_value"] == "A"
    )
    seller_a["raw_value_history"] = _history(100.0)

    report = validate_evidence(evidence)

    assert report["exit_code"] == 1
    gate = _gate(report, "sidecar_block_option_parity")
    assert gate["exit_code"] == 1
    assert any("option_labels_mismatch" in failure for failure in gate["failures"])


def test_zero_market_population_fails_instead_of_vacuously_passing() -> None:
    evidence = _valid_evidence()
    evidence["market_rows"] = []

    report = validate_evidence(evidence)

    gate = _gate(report, "general_dimension_parity")
    assert report["exit_code"] == 1
    assert gate["population"] == 0
    assert gate["exit_code"] == 1


def test_stale_or_mixed_block_epoch_fails() -> None:
    stale = _valid_evidence()
    stale["cohort"]["built_at_min"] = "2026-07-16T09:59:59Z"  # type: ignore[index]
    stale_report = validate_evidence(stale)

    mixed = _valid_evidence()
    mixed["block_rows"][0]["source_epoch"] = "epoch-old"  # type: ignore[index]
    mixed_report = validate_evidence(mixed)

    assert _gate(stale_report, "source_epoch_freshness")["exit_code"] == 1
    assert _gate(mixed_report, "source_epoch_freshness")["exit_code"] == 1


def test_one_newer_source_table_fails_even_when_other_sources_are_older() -> None:
    evidence = _valid_evidence()
    strategic_market = next(
        row
        for row in evidence["source_tables"]  # type: ignore[index]
        if row["logical_name"] == "strategic_market"
    )
    strategic_market["computed_at_max"] = "2026-07-16T10:03:00Z"

    report = validate_evidence(evidence)

    gate = _gate(report, "source_epoch_freshness")
    assert gate["exit_code"] == 1
    assert any("analysis_block_precedes_source" in failure for failure in gate["failures"])


def test_opaque_producer_epoch_is_not_recomputed_from_table_metadata() -> None:
    evidence = _valid_evidence()
    evidence["cohort"]["source_epoch"] = "producer-epoch-v2"  # type: ignore[index]
    evidence["block_rows"][0]["source_epoch"] = "producer-epoch-v2"  # type: ignore[index]

    report = validate_evidence(evidence)

    gate = _gate(report, "source_epoch_freshness")
    assert report["exit_code"] == 0
    assert gate["exit_code"] == 0


def test_source_freshness_query_uses_only_ubist_sales_rows() -> None:
    source = " ".join(inspect.getsource(post_reload_mart_gate._collect_source_tables).split())

    assert "WHERE source = %s AND measure = %s" in source
    assert '("ubist", "sales")' in source
    assert post_reload_mart_gate.EXPECTED_SOURCE_TABLES == {
        "general_brand": "mart_general_brand_metric",
        "general_market": "mart_general_market_metric",
        "general_dimension": "mart_general_filter_dimension_metric",
        "strategic_brand": "mart_strategic_ml_brand_metric",
        "strategic_market": "mart_strategic_ml_market_metric",
    }


def test_runtime_collection_is_read_only_and_has_no_fixed_reload_totals() -> None:
    source = inspect.getsource(post_reload_mart_gate)
    collector = inspect.getsource(post_reload_mart_gate.collect_runtime_evidence)

    assert "SET SESSION TRANSACTION READ ONLY" in collector
    assert "START TRANSACTION READ ONLY" in collector
    assert "connection.rollback()" in collector
    assert not re.search(r"\b(?:INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER|TRUNCATE)\b", collector)
    normalized = re.sub(r"[_,.\s]", "", source)
    assert "127504" not in normalized
    assert "82054" not in normalized


def test_empty_source_table_population_fails() -> None:
    evidence = _valid_evidence()
    evidence["source_tables"][0]["row_count"] = 0  # type: ignore[index]

    report = validate_evidence(evidence)

    gate = _gate(report, "source_epoch_freshness")
    assert gate["exit_code"] == 1
    assert any("source_table_population_empty" in failure for failure in gate["failures"])


def test_missing_source_table_timestamp_fails() -> None:
    evidence = _valid_evidence()
    evidence["source_tables"][0]["computed_at_max"] = None  # type: ignore[index]

    report = validate_evidence(evidence)

    gate = _gate(report, "source_epoch_freshness")
    assert gate["exit_code"] == 1
    assert any("source_table_timestamp_missing" in failure for failure in gate["failures"])


def test_inflated_analysis_block_fails() -> None:
    evidence = _valid_evidence()
    block = evidence["block_rows"][0]["analysis_levels_json"]  # type: ignore[index]
    block["data"]["판매사"]["by_channel"]["전체"][1]["value_series"] = [50.0]

    report = validate_evidence(evidence)

    assert _gate(report, "analysis_level_block_parity")["exit_code"] == 1


def test_block_period_census_mismatch_fails() -> None:
    evidence = _valid_evidence()
    block = evidence["block_rows"][0]["analysis_levels_json"]  # type: ignore[index]
    block["periods_monthly"] = ["2026-04", "2026-05"]

    report = validate_evidence(evidence)

    gate = _gate(report, "analysis_level_block_parity")
    assert gate["exit_code"] == 1
    assert any("block_period_census_mismatch" in failure for failure in gate["failures"])


def test_compensated_option_value_drift_fails_cross_contract_census() -> None:
    evidence = _valid_evidence()
    seller_rows = [
        row
        for row in evidence["dimension_rows"]  # type: ignore[index]
        if row["dimension_type"] == "seller"
    ]
    seller_rows[0]["raw_value_history"] = _history(39.0)
    seller_rows[1]["raw_value_history"] = _history(61.0)

    report = validate_evidence(evidence)

    assert _gate(report, "general_dimension_parity")["exit_code"] == 0
    gate = _gate(report, "sidecar_block_option_parity")
    assert gate["exit_code"] == 1
    assert any("option_value_mismatch" in failure for failure in gate["failures"])


def test_historical_dimension_drift_fails_even_when_latest_period_matches() -> None:
    evidence = _valid_evidence()
    evidence["market_rows"][0]["market_size_series"] = _history_periods(90.0, 100.0)  # type: ignore[index]
    for row in evidence["dimension_rows"]:  # type: ignore[index]
        row["raw_value_history"] = _history_periods(
            36.0 if row["dimension_value"] == "A" else 54.0,
            40.0 if row["dimension_value"] == "A" else 60.0,
        )
    evidence["dimension_rows"][0]["raw_value_history"] = _history_periods(46.0, 40.0)  # type: ignore[index]
    block = evidence["block_rows"][0]["analysis_levels_json"]  # type: ignore[index]
    block["periods_monthly"] = ["2026-04", "2026-05"]
    for level in LEVELS:
        block["data"][level] = {
            "segments": _segments_periods(),
            "by_channel": {"전체": _segments_periods()},
        }
    for key in ("general_specialty_rows", "strategic_specialty_rows"):
        row = evidence[key][0]  # type: ignore[index]
        row["metric_history"] = _history_periods(90.0, 100.0)
        row["specialty_data"] = {
            "내과": _history_periods(36.0, 40.0),
            "순환기": _history_periods(54.0, 60.0),
        }

    report = validate_evidence(evidence)

    gate = _gate(report, "general_dimension_parity")
    assert report["exit_code"] == 1
    assert any(":2026-04:" in failure for failure in gate["failures"])


def test_sidecar_covers_all_history_while_blocks_follow_latest_60_contract() -> None:
    evidence = _valid_evidence()
    periods = _monthly_periods(61)
    block_periods = periods[-60:]
    evidence["market_rows"][0]["market_size_series"] = _constant_history(periods, 100.0)  # type: ignore[index]
    for row in evidence["dimension_rows"]:  # type: ignore[index]
        value = 40.0 if row["dimension_value"] == "A" else 60.0
        row["raw_value_history"] = _constant_history(periods, value)
    block = evidence["block_rows"][0]["analysis_levels_json"]  # type: ignore[index]
    block["periods_monthly"] = block_periods
    for level in LEVELS:
        segments = _constant_segments(len(block_periods))
        block["data"][level] = {"segments": segments, "by_channel": {"전체": segments}}
    for key in ("general_specialty_rows", "strategic_specialty_rows"):
        row = evidence[key][0]  # type: ignore[index]
        row["metric_history"] = _constant_history(periods, 100.0)
        row["specialty_data"] = {
            "내과": _constant_history(periods, 40.0),
            "순환기": _constant_history(periods, 60.0),
        }

    report = validate_evidence(evidence)

    assert report["exit_code"] == 0
    assert _gate(report, "general_dimension_parity")["population"] == 61 * len(SIDECAR_DIMENSIONS)
    assert _gate(report, "analysis_level_block_parity")["population"] == 60 * len(LEVELS)

    earliest_drift = deepcopy(evidence)
    earliest_drift["dimension_rows"][0]["raw_value_history"][periods[0]] = {"raw_value": 41.0}  # type: ignore[index]
    drift_report = validate_evidence(earliest_drift)
    assert _gate(drift_report, "general_dimension_parity")["exit_code"] == 1
    assert _gate(drift_report, "analysis_level_block_parity")["exit_code"] == 0

    all_history_block = deepcopy(evidence)
    all_history_payload = all_history_block["block_rows"][0]["analysis_levels_json"]  # type: ignore[index]
    all_history_payload["periods_monthly"] = periods
    all_history_report = validate_evidence(all_history_block)
    assert _gate(all_history_report, "analysis_level_block_parity")["exit_code"] == 1


def test_missing_specialty_payload_fails_coverage() -> None:
    evidence = _valid_evidence()
    evidence["strategic_specialty_rows"][0]["specialty_data"] = {}  # type: ignore[index]

    report = validate_evidence(evidence)

    gate = _gate(report, "strategic_specialty_parity")
    assert gate["checked"] == 0
    assert gate["population"] == 1
    assert gate["exit_code"] == 1


def test_validator_does_not_depend_on_stale_fixed_totals() -> None:
    evidence = deepcopy(_valid_evidence())
    evidence["market_rows"][0]["market_size_series"] = _history(73_333.3)  # type: ignore[index]
    for row in evidence["dimension_rows"]:  # type: ignore[union-attr]
        value = 29_333.3 if row["dimension_value"] == "A" else 44_000.0
        row["raw_value_history"] = _history(value)
    for level in LEVELS:
        segments = evidence["block_rows"][0]["analysis_levels_json"]["data"][level]["by_channel"]["전체"]  # type: ignore[index]
        segments[0]["value_series"] = [73_333.3]
        segments[1]["value_series"] = [29_333.3]
        segments[2]["value_series"] = [44_000.0]
    for key in ("general_specialty_rows", "strategic_specialty_rows"):
        row = evidence[key][0]  # type: ignore[index]
        row["metric_history"] = _history(73_333.3)
        row["specialty_data"] = {"내과": _history(29_333.3), "순환기": _history(44_000.0)}

    report = validate_evidence(evidence)

    assert report["exit_code"] == 0


def test_cli_refuses_runtime_query_before_reload_completion() -> None:
    env = os.environ.copy()
    env.pop("MART_RELOAD_COMPLETE", None)

    result = _run_gate(env=env)

    assert result.returncode == 1
    assert "gate=mart_reload_authorization" in result.stdout


def test_cli_accepts_complete_evidence_and_prints_acceptance_fields(tmp_path: Path) -> None:
    evidence_path = tmp_path / "valid.json"
    _write_evidence(evidence_path, _valid_evidence())

    result = _run_gate("--evidence", str(evidence_path))

    assert result.returncode == 0
    assert "gate=general_dimension_parity" in result.stdout
    assert "checked=5 population=5" in result.stdout


def test_cli_rejects_inflated_evidence(tmp_path: Path) -> None:
    evidence = _valid_evidence()
    evidence["dimension_rows"][0]["raw_value_history"] = _history(50.0)  # type: ignore[index]
    evidence_path = tmp_path / "inflated.json"
    _write_evidence(evidence_path, evidence)

    result = _run_gate("--evidence", str(evidence_path))

    assert result.returncode == 1
    assert "dimension_total_mismatch" in result.stdout
