from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
from threading import Thread

import pytest

from pipeline.scripts.api.market_scope.archive_metrics import annual_ranking_payload
from pipeline.scripts.gates.release_acceptance import (
    check_brand_source_evidence,
    check_f116_correctness,
    check_goldens,
    check_market_growth_evidence,
    check_segment_sum_evidence,
)
from pipeline.scripts.gates.release_evidence import (
    BRAND_SOURCE_PROVENANCE,
    MARKET_GROWTH_PROVENANCE,
    SEGMENT_PROVENANCE,
    collect_brand_source_evidence,
    collect_market_growth_evidence,
    collect_segment_sum_evidence,
)


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "pipeline" / "scripts" / "gates" / "release_acceptance.py"


def _canonical_sha(payload: object) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(RUNNER), *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def _write_json(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def _golden_contract(identifier: str, payload: object) -> dict[str, object]:
    return {
        "id": identifier,
        "canonical_sha256": _canonical_sha(payload),
        "request": {"method": "GET", "path": f"/api/{identifier}"},
        "truth_basis": "fixture truth",
        "measured_at": "2026-07-14T00:00:00+09:00",
        "database": "fixture",
        "runtime_provenance": "sha256:fixture",
    }


@contextmanager
def _live_api(responses: dict[str, bytes]) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self._respond()

        def do_POST(self) -> None:
            self._respond()

        def _respond(self) -> None:
            body = responses.get(self.path)
            if body is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_strict_logs_fail_on_error_and_check_every_expected_pod(tmp_path: Path) -> None:
    pod_a = tmp_path / "pod-a.log"
    pod_b = tmp_path / "pod-b.log"
    pod_a.write_text("request completed 200\n", encoding="utf-8")
    pod_b.write_text("ERROR failed to build response\n", encoding="utf-8")

    result = _run(
        "strict-logs",
        "--expected-pod",
        "pod-a",
        "--expected-pod",
        "pod-b",
        "--pod-log",
        f"pod-a={pod_a}",
        "--pod-log",
        f"pod-b={pod_b}",
        "--environment",
        "failure-injection",
    )

    assert result.returncode == 1
    assert "pod-b:ERROR failed to build response" in result.stdout
    assert "gate=strict_logs" in result.stdout
    assert "checked=2" in result.stdout
    assert "population=2" in result.stdout
    assert "failures=1" in result.stdout
    assert "exit_code=1" in result.stdout


def test_population_gate_rejects_empty_candidates(tmp_path: Path) -> None:
    candidates = _write_json(tmp_path / "candidates.json", [])
    census = _write_json(
        tmp_path / "census.json",
        {"population": 5, "source": "independent mart SQL"},
    )

    result = _run(
        "population",
        "--candidates",
        str(candidates),
        "--census",
        str(census),
        "--environment",
        "failure-injection",
    )

    assert result.returncode == 1
    assert "empty population is a failure" in result.stdout
    assert "gate=population" in result.stdout
    assert "checked=0" in result.stdout
    assert "population=5" in result.stdout
    assert "failures=5" in result.stdout
    assert "exit_code=1" in result.stdout


@pytest.mark.parametrize(
    "target",
    ["brands", "market_status", "cause_livalo", "dynamic_general_c10a1_livalo"],
)
def test_golden_gate_rejects_corrupted_expected_hash_for_each_identity(
    tmp_path: Path,
    target: str,
) -> None:
    payloads = {
        identifier: {"id": identifier}
        for identifier in ("brands", "market_status", "cause_livalo", "dynamic_general_c10a1_livalo")
    }
    contracts = [_golden_contract(identifier, payload) for identifier, payload in payloads.items()]
    target_contract = next(contract for contract in contracts if contract["id"] == target)
    target_contract["canonical_sha256"] = "0" * 64
    contracts_path = _write_json(tmp_path / "contracts.json", {"contracts": contracts})
    responses = {
        f"/api/{identifier}": json.dumps(payload).encode()
        for identifier, payload in payloads.items()
    }

    with _live_api(responses) as base_url:
        result = check_goldens(contracts_path, base_url, "failure-injection")

    assert result.exit_code == 1
    assert result.checked == 4
    assert result.population == 4
    assert result.failures == 1
    assert any(target in detail for detail in result.details)


def test_golden_gate_rejects_unreachable_live_api(tmp_path: Path) -> None:
    payload = {"ok": True}
    contracts_path = _write_json(
        tmp_path / "contracts.json", {"contracts": [_golden_contract("required", payload)]}
    )

    result = check_goldens(contracts_path, "http://127.0.0.1:1", "failure-injection")

    assert result.exit_code == 1
    assert result.checked == 1
    assert result.population == 1
    assert result.failures == 1
    assert any("live request failed" in detail for detail in result.details)


def test_golden_gate_rejects_empty_live_response(tmp_path: Path) -> None:
    payload = {"ok": True}
    contracts_path = _write_json(
        tmp_path / "contracts.json", {"contracts": [_golden_contract("required", payload)]}
    )

    with _live_api({"/api/required": b""}) as base_url:
        result = check_goldens(contracts_path, base_url, "failure-injection")

    assert result.exit_code == 1
    assert result.checked == 1
    assert result.population == 1
    assert result.failures == 1
    assert any("empty response body" in detail for detail in result.details)


def test_golden_cli_rejects_untracked_contract_and_observation_files(tmp_path: Path) -> None:
    result = _run(
        "goldens",
        "--base-url",
        "http://127.0.0.1:1",
        "--contracts",
        str(tmp_path / "contracts.json"),
        "--observations",
        str(tmp_path / "observations.json"),
    )

    assert result.returncode == 2
    assert "unrecognized arguments" in result.stderr


def test_golden_cli_fails_when_tracked_live_requests_are_unreachable() -> None:
    result = _run(
        "goldens",
        "--base-url",
        "http://127.0.0.1:1",
        "--timeout-seconds",
        "0.1",
        "--environment",
        "failure-injection",
    )

    assert result.returncode == 1
    assert "checked=4" in result.stdout
    assert "population=4" in result.stdout
    assert "failures=4" in result.stdout
    assert "exit_code=1" in result.stdout


def test_growth_windows_gate_rejects_identical_windows(tmp_path: Path) -> None:
    window = {
        "period_start": "2021-01",
        "period_end": "2026-05",
        "market_start": 100.0,
        "market_end": 130.0,
        "market_growth": 30.0,
        "by_brand": {
            "top_contributors": [{"contribution_value": 30.0}],
            "others_total": 0.0,
        },
        "by_company": {
            "top_contributors": [{"contribution_value": 30.0}],
            "others_total": 0.0,
        },
    }
    evidence = _write_json(
        tmp_path / "growth.json",
        {
            "classification": "sample",
            "cases": [
                {
                    "id": "A10N1|ubist|sales",
                    "expected_period_starts": {
                        "1y": "2025-06",
                        "2y": "2024-06",
                        "3y": "2023-06",
                        "4y": "2022-06",
                        "5y": "2021-06",
                    },
                    "expected_market_starts": {
                        key: float(index)
                        for index, key in enumerate(
                            ("1y", "2y", "3y", "4y", "5y"),
                            start=1,
                        )
                    },
                    "expected_truncated_windows": [],
                    "windows": {key: dict(window) for key in ("1y", "2y", "3y", "4y", "5y")},
                }
            ],
        },
    )

    result = _run(
        "growth-windows",
        "--evidence",
        str(evidence),
        "--environment",
        "failure-injection",
    )

    assert result.returncode == 1
    assert "gate=growth_windows" in result.stdout
    assert "classification=sample" in result.stdout
    assert "failures=" in result.stdout
    assert "exit_code=1" in result.stdout


def test_growth_windows_gate_rejects_duplicate_complete_windows_when_later_windows_are_truncated(
    tmp_path: Path,
) -> None:
    keys = ("1y", "2y", "3y", "4y", "5y")
    starts = ("2025-06", "2024-06", "2023-01", "2023-01", "2023-01")
    windows = {}
    for key, start in zip(keys, starts, strict=True):
        contribution = 10.0 if key in {"1y", "2y"} else 20.0
        windows[key] = {
            "period_start": start,
            "period_end": "2026-05",
            "market_start": 100.0,
            "market_end": 100.0 + contribution,
            "market_growth": contribution,
            "by_brand": {
                "top_contributors": [{"contribution_value": contribution}],
                "others_total": 0.0,
            },
            "by_company": {
                "top_contributors": [{"contribution_value": contribution}],
                "others_total": 0.0,
            },
        }
        if key in {"3y", "4y", "5y"}:
            windows[key]["period_start_actual"] = start
            windows[key]["reason"] = "earliest_available"
    evidence = _write_json(
        tmp_path / "growth.json",
        {
            "classification": "sample",
            "cases": [
                {
                    "id": "A10N1|ubist|sales",
                    "expected_period_starts": dict(zip(keys, starts, strict=True)),
                    "expected_market_starts": {key: 100.0 for key in keys},
                    "expected_truncated_windows": ["3y", "4y", "5y"],
                    "windows": windows,
                }
            ],
        },
    )

    result = _run("growth-windows", "--evidence", str(evidence), "--environment", "mixed-history")

    assert result.returncode == 1
    assert "non-truncated contribution payloads are not distinct" in result.stdout


def test_growth_windows_gate_accepts_distinct_reconciled_windows(tmp_path: Path) -> None:
    keys = ("1y", "2y", "3y", "4y", "5y")
    starts = ("2025-06", "2024-06", "2023-06", "2022-06", "2021-06")
    windows = {}
    for index, (key, start) in enumerate(zip(keys, starts, strict=True), start=1):
        market_start = float(index * 10)
        contribution = float(index)
        windows[key] = {
            "period_start": start,
            "period_end": "2026-05",
            "market_start": market_start,
            "market_end": market_start + contribution,
            "market_growth": contribution,
            "by_brand": {
                "top_contributors": [{"contribution_value": contribution}],
                "others_total": 0.0,
            },
            "by_company": {
                "top_contributors": [{"contribution_value": contribution}],
                "others_total": 0.0,
            },
        }
    evidence = _write_json(
        tmp_path / "growth.json",
        {
            "classification": "sample",
            "cases": [
                {
                    "id": "A10N1|ubist|sales",
                    "expected_period_starts": dict(zip(keys, starts, strict=True)),
                    "expected_market_starts": {
                        key: float(index * 10) for index, key in enumerate(keys, start=1)
                    },
                    "expected_truncated_windows": [],
                    "windows": windows,
                }
            ],
        },
    )

    result = _run("growth-windows", "--evidence", str(evidence), "--environment", "local")

    assert result.returncode == 0
    assert "gate=growth_windows" in result.stdout
    assert "checked=1" in result.stdout
    assert "population=1" in result.stdout
    assert "failures=0" in result.stdout
    assert "exit_code=0" in result.stdout


def _period_range_evidence(*, identical_windows: bool = False) -> dict[str, object]:
    value_b = 10.0 if identical_windows else 20.0
    window_a = {
        "start": "2025-01",
        "end": "2025-12",
        "payload": {"market_size_series": [{"period": "2025-01", "value": 10.0}]},
    }
    window_b = {
        "start": "2026-01",
        "end": "2026-04",
        "payload": {"market_size_series": [{"period": "2026-01", "value": value_b}]},
    }
    return {
        "classification": "census",
        "cases": [
            {
                "id": "general|C10A1|ubist|sales",
                "window_a": window_a,
                "window_b": window_b,
                "window_a_repeat": window_a,
            }
        ],
    }


def test_period_ranges_gate_accepts_complete_distinct_cross_call_evidence(tmp_path: Path) -> None:
    evidence = _write_json(tmp_path / "periods.json", _period_range_evidence())

    result = _run("period-ranges", "--evidence", str(evidence), "--environment", "local")

    assert result.returncode == 0
    assert "gate=period_ranges" in result.stdout
    assert "classification=census" in result.stdout
    assert "checked=3" in result.stdout
    assert "population=3" in result.stdout
    assert "failures=0" in result.stdout
    assert "exit_code=0" in result.stdout


def test_period_ranges_gate_rejects_ignored_window_failure_injection(tmp_path: Path) -> None:
    evidence = _write_json(
        tmp_path / "periods_ignored.json",
        _period_range_evidence(identical_windows=True),
    )

    result = _run(
        "period-ranges",
        "--evidence",
        str(evidence),
        "--environment",
        "failure-injection",
    )

    assert result.returncode == 1
    assert "period windows produced identical values" in result.stdout
    assert "failures=1" in result.stdout
    assert "exit_code=1" in result.stdout


def test_tracked_golden_contracts_have_exact_identity_set_and_truth_metadata() -> None:
    document = json.loads(
        (ROOT / "tests" / "api" / "api_golden_contracts.json").read_text(encoding="utf-8")
    )
    contracts = document["contracts"]

    assert {contract["id"] for contract in contracts} == {
        "brands",
        "market_status",
        "cause_livalo",
        "dynamic_general_c10a1_livalo",
    }
    assert len(contracts) == 4
    assert next(contract for contract in contracts if contract["id"] == "brands")["canonical_sha256"] == (
        "77917362f9ca356bc6a596abcb59d5b7b8e418c45ca7599d5c618852866fa6ab"
    )
    for contract in contracts:
        assert len(contract["canonical_sha256"]) == 64
        assert contract["request"]["method"] in {"GET", "POST"}
        assert contract["request"]["path"].startswith("/jw-market-backend-api/api/")
        assert contract["truth_basis"]
        assert contract["measured_at"]
        assert contract["database"] == "jw_mart_d2_stage_20260630_r2"
        assert contract["runtime_provenance"]


def test_golden_gate_passes_only_when_all_live_responses_match(tmp_path: Path) -> None:
    payloads = {"a": {"value": 1}, "b": {"value": 2}}
    contracts = [
        {
            "id": identifier,
            "canonical_sha256": _canonical_sha(payload),
            "request": {"method": "GET", "path": f"/api/{identifier}"},
            "truth_basis": "fixture truth",
            "measured_at": "2026-07-14T00:00:00+09:00",
            "database": "fixture",
            "runtime_provenance": "sha256:fixture",
        }
        for identifier, payload in payloads.items()
    ]
    contracts_path = _write_json(tmp_path / "contracts.json", {"contracts": contracts})
    responses = {
        f"/api/{identifier}": json.dumps(payload).encode()
        for identifier, payload in payloads.items()
    }

    with _live_api(responses) as base_url:
        result = check_goldens(contracts_path, base_url, "failure-injection")

    assert result.exit_code == 0
    assert result.checked == 2
    assert result.population == 2
    assert result.failures == 0


def test_segment_sum_sample_requires_every_expected_level(tmp_path: Path) -> None:
    result = check_segment_sum_evidence(
        {
            "classification": "sample",
            "provenance": SEGMENT_PROVENANCE,
            "observations": [
                {"level": "class", "segment_sum": 100.0, "market_total": 100.0},
            ],
        },
        0.01,
        "failure-injection",
    )

    assert result.exit_code == 1
    assert "missing levels:" in "\n".join(result.details)
    assert result.checked == 1
    assert result.population == 3


def test_segment_sum_uses_absolute_tolerance_without_hiding_missing_levels(tmp_path: Path) -> None:
    result = check_segment_sum_evidence(
        {
            "classification": "census",
            "provenance": SEGMENT_PROVENANCE,
            "observations": [
                {"level": "class", "segment_sum": 100.009, "market_total": 100.0},
                {"level": "molecule", "segment_sum": 100.0, "market_total": 100.0},
                {"level": "ox_gx", "segment_sum": 100.0, "market_total": 100.0},
            ],
        },
        0.01,
        "local",
    )

    assert result.exit_code == 0
    assert result.tolerance == "abs_tol=0.01,rel_tol=0"
    assert result.failures == 0


def test_segment_sum_cli_rejects_caller_supplied_evidence_files(tmp_path: Path) -> None:
    result = _run(
        "segment-sum",
        "--base-url",
        "http://127.0.0.1:1",
        "--expected-identities",
        str(tmp_path / "expected.json"),
        "--observations",
        str(tmp_path / "observations.json"),
    )

    assert result.returncode == 2
    assert "unrecognized arguments" in result.stderr


def test_segment_sum_runtime_collector_compares_live_payload_to_sql_total() -> None:
    def fake_fetch(base_url: str, path: str, **kwargs: object) -> object:
        assert base_url == "http://runtime"
        assert path.startswith("/jw-market-backend-api/api/cause/")
        return {
            "data": {
                "analysis_levels": {
                    "data": {
                        level: {"segments": [{"name": "전체", "value": 100.0}, {"name": "A", "value": 100.0}]}
                        for level in ("Class", "Molecule", "Ox/Gx")
                    }
                }
            }
        }

    def fake_sql(query: str, params: dict[str, object], **kwargs: object) -> list[dict[str, object]]:
        assert "mart_strategic_ml_market_metric" in query
        assert "WHERE ml_id = %(ml_id)s" in query
        assert params["ml_id"] == "ml_006"
        return [{"market_size_series": json.dumps({"2026-05": 100.0})}]

    evidence = collect_segment_sum_evidence(
        "http://runtime",
        timeout_seconds=1.0,
        env={},
        fetcher=fake_fetch,
        sql_fetcher=fake_sql,
    )
    result = check_segment_sum_evidence(evidence, 0.01, "unit")

    assert evidence["provenance"] == SEGMENT_PROVENANCE
    assert result.exit_code == 0


def test_segment_sum_rejects_expected_perturbation_failure_injection() -> None:
    evidence = {
        "classification": "census",
        "provenance": SEGMENT_PROVENANCE,
        "observations": [
            {"level": "class", "segment_sum": 99.0, "market_total": 100.0},
            {"level": "molecule", "segment_sum": 100.0, "market_total": 100.0},
            {"level": "ox_gx", "segment_sum": 100.0, "market_total": 100.0},
        ],
    }

    result = check_segment_sum_evidence(evidence, 0.01, "failure-injection")

    assert result.exit_code == 1
    assert any("class: sum mismatch" in detail for detail in result.details)


def test_segment_sum_rejects_non_finite_values_failure_injection() -> None:
    evidence = {
        "classification": "census",
        "provenance": SEGMENT_PROVENANCE,
        "observations": [
            {"level": "class", "segment_sum": float("nan"), "market_total": 100.0},
            {"level": "molecule", "segment_sum": 100.0, "market_total": 100.0},
            {"level": "ox_gx", "segment_sum": 100.0, "market_total": 100.0},
        ],
    }

    result = check_segment_sum_evidence(evidence, 0.01, "failure-injection")

    assert result.exit_code == 1
    assert any("class: non-finite numeric observation" in detail for detail in result.details)


def test_market_growth_runtime_collector_recomputes_formula_from_sql() -> None:
    series = {
        f"{2021 + (index + 4) // 12:04d}-{(index + 4) % 12 + 1:02d}": 100.0
        for index in range(61)
    }
    series["2026-05"] = 110.0

    def fake_sql(query: str, params: dict[str, object], **kwargs: object) -> list[dict[str, object]]:
        assert "mart_general_market_metric" in query
        return [{"source": "ubist", "atc4_code": "A10N1", "market_size_series": json.dumps(series)}]

    def fake_fetch(base_url: str, path: str, **kwargs: object) -> object:
        assert path == "/jw-market-backend-api/api/dynamic-market"
        assert kwargs["body"] == {
            "view": "general",
            "source": "ubist",
            "measure": "sales",
            "filters": {"atc4": ["A10N1"]},
        }
        return {
            "result": {
                "data": {
                    "market_size_series": [
                        {
                            "period": "2026-05",
                            "mom_growth_pct": ((110.0 / 100.0) ** (1 / 60) - 1) * 100,
                        }
                    ]
                }
            }
        }

    evidence = collect_market_growth_evidence(
        "http://runtime",
        timeout_seconds=1.0,
        max_workers=1,
        env={},
        fetcher=fake_fetch,
        sql_fetcher=fake_sql,
    )
    result = check_market_growth_evidence(evidence, 1, 0.0001, "unit")

    assert evidence["provenance"] == MARKET_GROWTH_PROVENANCE
    assert result.exit_code == 0


def test_market_growth_runtime_collector_uses_latest_available_endpoint() -> None:
    expected = ((121.0 / 100.0) ** (1 / 60) - 1) * 100
    series = {
        "2021-05": None,
        "2022-05": 100.0,
        "2026-05": 121.0,
        "2026-06": None,
    }

    def fake_sql(query: str, params: dict[str, object], **kwargs: object) -> list[dict[str, object]]:
        return [{"source": "ubist", "atc4_code": "A10N1", "market_size_series": series}]

    def fake_fetch(base_url: str, path: str, **kwargs: object) -> object:
        return {
            "result": {
                "data": {
                    "market_size_series": [
                        {"period": "2022-05", "mom_growth_pct": None},
                        {"period": "2026-05", "mom_growth_pct": expected},
                    ]
                }
            }
        }

    evidence = collect_market_growth_evidence(
        "http://runtime",
        timeout_seconds=1.0,
        max_workers=1,
        env={},
        fetcher=fake_fetch,
        sql_fetcher=fake_sql,
    )

    observation = evidence["observations"][0]
    assert observation["expected"] == pytest.approx(expected)
    assert observation["expected_end_period"] == "2026-05"
    assert observation["expected_baseline_period"] == "2022-05"
    assert check_market_growth_evidence(evidence, 1, 0.0001, "unit").exit_code == 0


def test_market_growth_rejects_negative_100_failure_injection() -> None:
    evidence = {
        "classification": "census",
        "provenance": MARKET_GROWTH_PROVENANCE,
        "observations": [
            {"source": "ubist", "market": "A10N1", "expected": 1.0, "actual": -100.0, "error": None}
        ],
    }

    result = check_market_growth_evidence(evidence, 1, 0.0001, "failure-injection")

    assert result.exit_code == 1
    assert any("-100 growth sentinel is forbidden" in detail for detail in result.details)


@pytest.mark.parametrize("actual", [-100.0, -99.9999])
def test_market_growth_rejects_negative_100_with_numeric_tolerance(actual: float) -> None:
    evidence = {
        "classification": "census",
        "provenance": MARKET_GROWTH_PROVENANCE,
        "observations": [
            {"source": "ubist", "market": "A10N1", "expected": actual, "actual": actual, "error": None}
        ],
    }

    result = check_market_growth_evidence(evidence, 1, 0.0001, "failure-injection")

    assert result.exit_code == 1
    assert any("-100 growth sentinel is forbidden" in detail for detail in result.details)


@pytest.mark.parametrize(
    ("actual", "expected", "expected_exit"),
    [(29.52, 29.52, 0), (29.53, 29.52, 1)],
)
def test_market_growth_uses_numeric_boundaries(
    actual: float,
    expected: float,
    expected_exit: int,
) -> None:
    evidence = {
        "classification": "census",
        "provenance": MARKET_GROWTH_PROVENANCE,
        "observations": [
            {"source": "ubist", "market": "A10N1", "expected": expected, "actual": actual, "error": None}
        ],
    }

    result = check_market_growth_evidence(evidence, 1, 0.0001, "failure-injection")

    assert result.exit_code == expected_exit


def test_market_growth_rejects_independent_expected_perturbation() -> None:
    evidence = {
        "classification": "census",
        "provenance": MARKET_GROWTH_PROVENANCE,
        "observations": [
            {"source": "ubist", "market": "A10N1", "expected": 1.0, "actual": 2.0, "error": None}
        ],
    }

    result = check_market_growth_evidence(evidence, 1, 0.0001, "failure-injection")

    assert result.exit_code == 1
    assert any("growth mismatch" in detail for detail in result.details)


def test_market_growth_rejects_missing_independent_expected_value() -> None:
    evidence = {
        "classification": "census",
        "provenance": MARKET_GROWTH_PROVENANCE,
        "observations": [
            {
                "source": "ubist",
                "market": "A10N1",
                "expected": None,
                "actual": None,
                "error": None,
            }
        ],
    }

    result = check_market_growth_evidence(evidence, 1, 0.0001, "failure-injection")

    assert result.exit_code == 1
    assert any("independent expected value is unavailable" in detail for detail in result.details)


def test_market_growth_maps_iqvia_database_source_to_public_request_source() -> None:
    series = {f"2021-Q{index:02d}": 100.0 for index in range(21)}
    series["2021-Q20"] = 121.0

    def fake_sql(query: str, params: dict[str, object], **kwargs: object) -> list[dict[str, object]]:
        return [{"source": "iqvia_nsa", "atc4_code": "A10N1", "market_size_series": series}]

    def fake_fetch(base_url: str, path: str, **kwargs: object) -> object:
        assert kwargs["body"] == {
            "view": "general",
            "source": "iqvia",
            "measure": "sales",
            "filters": {"atc4": ["A10N1"]},
        }
        return {
            "result": {
                "data": {
                    "market_size_series": [
                        {
                            "period": "2021-Q20",
                            "mom_growth_pct": ((121.0 / 100.0) ** (1 / 20) - 1) * 100,
                        }
                    ]
                }
            }
        }

    evidence = collect_market_growth_evidence(
        "http://runtime",
        timeout_seconds=1.0,
        max_workers=1,
        env={},
        fetcher=fake_fetch,
        sql_fetcher=fake_sql,
    )

    assert check_market_growth_evidence(evidence, 1, 0.0001, "unit").exit_code == 0


def test_runtime_collection_error_is_rendered_as_fail_closed_gate_result() -> None:
    result = _run(
        "segment-sum",
        "--base-url",
        "http://127.0.0.1:1",
        "--timeout-seconds",
        "0.01",
        "--environment",
        "failure-injection",
    )

    assert result.returncode == 1
    assert "gate=segment_sum" in result.stdout
    assert "checked=0" in result.stdout
    assert "failures=1" in result.stdout
    assert "exit_code=1" in result.stdout
    assert "Traceback" not in result.stderr


def test_competition_ranking_gate_checks_every_entity_year(tmp_path: Path) -> None:
    observations = _write_json(tmp_path / "rankings.json", _competition_rankings())

    result = _run(
        "competition-ranking",
        "--observations",
        str(observations),
        "--abs-tol",
        "0.01",
        "--expected-year",
        "2025",
        "--expected-year",
        "2026",
        "--environment",
        "fixture",
    )

    assert result.returncode == 0
    assert "gate=competition_ranking" in result.stdout
    assert "classification=census" in result.stdout
    assert "checked=4" in result.stdout
    assert "population=4" in result.stdout
    assert "failures=0" in result.stdout
    assert "exit_code=0" in result.stdout


def test_competition_ranking_gate_accepts_market_scope_brand_ids_with_display_names(
    tmp_path: Path,
) -> None:
    histories = {
        "id-a": {"2025-01": 50.0, "2026-01": 60.0},
        "id-b": {"2025-01": 30.0, "2026-01": 20.0},
        "id-c": {"2025-01": 20.0, "2026-01": 20.0},
    }
    observations = {
        "brand": annual_ranking_payload(
            histories,
            label_key="brand_key",
            focus_id="id-a",
            display_names={"id-a": "Brand A", "id-b": "Brand B", "id-c": "Brand C"},
        ),
        "company": annual_ranking_payload(
            histories,
            label_key="company",
            focus_id="id-a",
        ),
    }

    result = _run(
        "competition-ranking",
        "--observations",
        str(_write_json(tmp_path / "rankings.json", observations)),
        "--abs-tol",
        "0.01",
        "--expected-year",
        "2025",
        "--expected-year",
        "2026",
        "--environment",
        "market-scope-fixture",
    )

    assert result.returncode == 0
    assert "checked=4" in result.stdout
    assert "failures=0" in result.stdout
    assert "exit_code=0" in result.stdout


def test_competition_ranking_gate_fails_when_one_rank_is_removed(tmp_path: Path) -> None:
    observations = _competition_rankings()
    del observations["brand"]["yearly"][0]["rankings"][1]

    result = _run(
        "competition-ranking",
        "--observations",
        str(_write_json(tmp_path / "rankings.json", observations)),
        "--abs-tol",
        "0.01",
        "--expected-year",
        "2025",
        "--expected-year",
        "2026",
        "--environment",
        "failure-injection",
    )

    assert result.returncode == 1
    assert "brand|2025: non-contiguous ranks got=1,3 expected=1,2" in result.stdout
    assert "failures=3" in result.stdout
    assert "exit_code=1" in result.stdout


def test_competition_ranking_gate_fails_when_yearly_diverges_from_rankings_by_year(tmp_path: Path) -> None:
    observations = _competition_rankings()
    observations["brand"]["yearly"][0]["rankings"].pop(1)

    result = _run(
        "competition-ranking",
        "--observations",
        str(_write_json(tmp_path / "rankings.json", observations)),
        "--abs-tol",
        "0.01",
        "--expected-year",
        "2025",
        "--expected-year",
        "2026",
        "--environment",
        "failure-injection",
    )

    assert result.returncode == 1
    assert "brand|2025: yearly rankings diverge from rankings_by_year prefix" in result.stdout
    assert "exit_code=1" in result.stdout


@pytest.mark.parametrize("entity", ("brand", "company"))
def test_competition_ranking_gate_fails_when_real_zero_row_is_removed(
    tmp_path: Path,
    entity: str,
) -> None:
    observations = _competition_rankings()
    rows = observations[entity]["yearly"][0]["rankings"]
    rows.pop(next(index for index, row in enumerate(rows) if row.get("value") == 0.0))

    result = _run(
        "competition-ranking",
        "--observations",
        str(_write_json(tmp_path / "rankings.json", observations)),
        "--abs-tol",
        "0.01",
        "--expected-year",
        "2025",
        "--expected-year",
        "2026",
        "--environment",
        "failure-injection",
    )

    assert result.returncode == 1
    assert f"{entity}|2025: yearly rankings diverge from rankings_by_year prefix" in result.stdout
    assert "exit_code=1" in result.stdout


@pytest.mark.parametrize("entity", ("brand", "company"))
def test_competition_ranking_gate_fails_when_selected_tail_is_folded_into_others(
    tmp_path: Path,
    entity: str,
) -> None:
    observations = _competition_rankings()
    rows = observations[entity]["yearly"][0]["rankings"]
    removed = rows.pop(2)
    others = next(row for row in rows if row.get("is_others"))
    others["value"] += removed["value"]
    others["ms_pct"] += removed["ms_pct"]

    result = _run(
        "competition-ranking",
        "--observations",
        str(_write_json(tmp_path / "rankings.json", observations)),
        "--abs-tol",
        "0.01",
        "--expected-year",
        "2025",
        "--expected-year",
        "2026",
        "--environment",
        "failure-injection",
    )

    assert result.returncode == 1
    assert f"{entity}|2025: yearly rankings diverge from rankings_by_year prefix" in result.stdout
    assert "exit_code=1" in result.stdout


def _competition_rankings() -> dict[str, object]:
    def year(year_value: int, labels: tuple[str, str, str]) -> dict[str, object]:
        return {
            "year": year_value,
            "rankings": [
                {labels[0]: "A", "rank": 1, "value": 50.0, "ms_pct": 50.0, "is_others": False},
                {labels[0]: "B", "rank": 2, "value": 30.0, "ms_pct": 30.0, "is_others": False},
                {labels[0]: "C", "rank": 3, "value": 10.0, "ms_pct": 10.0, "is_others": False},
                {labels[0]: "Zero", "rank": None, "value": 0.0, "ms_pct": 0.0, "is_others": False},
                {labels[0]: labels[1], "rank": None, "value": 10.0, "ms_pct": 10.0, "is_others": True},
            ],
        }

    brand_yearly = [year(2025, ("brand", "기타", "company")), year(2026, ("brand", "기타", "company"))]
    company_yearly = [year(2025, ("company", "기타", "brand")), year(2026, ("company", "기타", "brand"))]
    return {
        "brand": {
            "yearly": brand_yearly,
            "top_brands": ["A", "B", "C", "Zero", "기타"],
            "rankings_by_year": {
                str(item["year"]): [dict(row) for row in item["rankings"]]
                for item in brand_yearly
            },
        },
        "company": {
            "yearly": company_yearly,
            "top_brands": ["A", "B", "C", "Zero", "기타"],
            "rankings_by_year": {
                str(item["year"]): [dict(row) for row in item["rankings"]]
                for item in company_yearly
            },
        },
    }


def test_gate_sources_do_not_contain_known_fail_open_shell_patterns() -> None:
    gate_root = ROOT / "pipeline" / "scripts" / "gates"
    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in gate_root.rglob("*")
        if path.is_file() and path.suffix in {".py", ".sh", ".md", ".json"}
    )

    assert "|| true" not in sources
    assert "head -1" not in sources
    assert "head -n 1" not in sources


def _brand_source_expectations() -> dict[str, object]:
    return {
        "classification": "census",
        "brands": ["리바로", "위너프A+"],
        "views": ["general", "strategic"],
        "sources": ["UBIST", "IQVIA"],
    }


def _brand_source_observations() -> list[dict[str, object]]:
    available = {
        ("리바로", "general", "UBIST"),
        ("리바로", "general", "IQVIA"),
        ("리바로", "strategic", "UBIST"),
        ("위너프A+", "strategic", "IQVIA"),
    }
    return [
        {
            "brand": brand,
            "view": view,
            "source": source,
            "listed": (brand, view, source) in available,
            "has_data": (brand, view, source) in available,
        }
        for brand in ("리바로", "위너프A+")
        for view in ("general", "strategic")
        for source in ("UBIST", "IQVIA")
    ]


def test_brand_sources_gate_requires_bidirectional_census_parity(tmp_path: Path) -> None:
    result = check_brand_source_evidence(
        {
            "classification": "census",
            "provenance": BRAND_SOURCE_PROVENANCE,
            "observations": _brand_source_observations(),
        },
        8,
        "fixture",
    )

    assert result.exit_code == 0
    assert result.checked == 8
    assert result.population == 8
    assert result.failures == 0


def test_brand_sources_gate_failure_injection_exits_one(tmp_path: Path) -> None:
    observations = _brand_source_observations()
    observations[0]["listed"] = not observations[0]["has_data"]
    result = check_brand_source_evidence(
        {
            "classification": "census",
            "provenance": BRAND_SOURCE_PROVENANCE,
            "observations": observations,
        },
        8,
        "failure-injection",
    )

    assert result.exit_code == 1
    assert any("listed/data mismatch" in detail for detail in result.details)
    assert result.failures == 1


def test_brand_sources_gate_rejects_empty_or_incomplete_population(tmp_path: Path) -> None:
    result = check_brand_source_evidence(
        {"classification": "census", "provenance": BRAND_SOURCE_PROVENANCE, "observations": []},
        8,
        "failure-injection",
    )

    assert result.exit_code == 1
    assert any("empty observation population is a failure" in detail for detail in result.details)
    assert result.checked == 0
    assert result.population == 8


def test_brand_sources_cli_rejects_caller_supplied_evidence_files(tmp_path: Path) -> None:
    result = _run(
        "brand-sources",
        "--base-url",
        "http://127.0.0.1:1",
        "--expectations",
        str(tmp_path / "expectations.json"),
        "--observations",
        str(tmp_path / "observations.json"),
    )

    assert result.returncode == 2
    assert "unrecognized arguments" in result.stderr


def test_brand_sources_runtime_collector_probes_each_listed_context() -> None:
    deep_paths: list[str] = []

    def fake_fetch(base_url: str, path: str, **kwargs: object) -> object:
        if path == "/jw-market-backend-api/api/brands":
            return [{"brand": "리바로", "general_sources": ["UBIST"], "strategic_sources": []}]
        if path.startswith("/jw-market-backend-api/api/brands?"):
            return [
                {
                    "brand": "리바로",
                    "contexts": [
                        {"view_kind": "general", "market_id": "C10A1", "has_market_data": True},
                        {"view_kind": "strategic_ml", "market_id": "ml_006", "has_market_data": True},
                    ],
                }
            ]
        deep_paths.append(path)
        assert path.startswith("/jw-market-backend-api/api/deep-analysis/")
        assert "view=" not in path
        assert "view_kind=" in path
        assert "market_id=" in path
        assert "source=" in path
        if "view_kind=general" in path and "source=ubist" in path:
            return {"market_meta": {"has_market_data": True}}
        return {"detail": {"error": "source_not_available"}}

    evidence = collect_brand_source_evidence(
        "http://runtime",
        timeout_seconds=1.0,
        max_workers=1,
        fetcher=fake_fetch,
    )
    result = check_brand_source_evidence(evidence, 4, "unit")

    assert evidence["provenance"] == BRAND_SOURCE_PROVENANCE
    assert result.exit_code == 0
    assert deep_paths


def test_gate_runtime_evidence_producer_is_tracked() -> None:
    result = subprocess.run(
        ["git", "ls-files", "pipeline/scripts/gates/release_evidence.py"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == "pipeline/scripts/gates/release_evidence.py"


def _cause_assembly_evidence(tmp_path: Path, *, mutate_after: bool = False) -> Path:
    before = tmp_path / "before.json"
    after = tmp_path / "after.json"
    before.write_bytes(b'{"value":1,"missing":null}')
    after.write_bytes(b'{"value":2,"missing":null}' if mutate_after else before.read_bytes())
    expected_cases = [
        "miss|jw25|livalo",
        "hit|jw25|livalo",
        "miss|expanded|mounjaro",
        "hit|expanded|mounjaro",
    ]
    return _write_json(
        tmp_path / "cause_assembly.json",
        {
            "classification": "census",
            "max_after_ms": 2000.0,
            "expected_cases": expected_cases,
            "cache_expanded": False,
            "cases": [
                {
                    "id": identifier,
                    "before_payload": before.name,
                    "after_payload": after.name,
                    "before_ms": 1800.0,
                    "after_ms": 900.0,
                }
                for identifier in expected_cases
            ],
        },
    )


def test_cause_assembly_gate_requires_byte_identity_and_faster_census(tmp_path: Path) -> None:
    result = _run(
        "cause-assembly",
        "--evidence",
        str(_cause_assembly_evidence(tmp_path)),
        "--environment",
        "local-runtime",
    )

    assert result.returncode == 0
    assert "gate=cause_assembly" in result.stdout
    assert "classification=census" in result.stdout
    assert "checked=4" in result.stdout
    assert "population=4" in result.stdout
    assert "failures=0" in result.stdout
    assert "exit_code=0" in result.stdout


def test_cause_assembly_gate_failure_injection_exits_one(tmp_path: Path) -> None:
    result = _run(
        "cause-assembly",
        "--evidence",
        str(_cause_assembly_evidence(tmp_path, mutate_after=True)),
        "--environment",
        "failure-injection",
    )

    assert result.returncode == 1
    assert "byte mismatch" in result.stdout
    assert "failures=4" in result.stdout
    assert "exit_code=1" in result.stdout


def _cause_null_integrity_evidence(
    tmp_path: Path,
    *,
    invent_zero: bool = False,
    regress_performance: bool = False,
    extreme_numeric: bool = False,
    census_mismatch: bool = False,
) -> Path:
    return _write_json(
        tmp_path / "cause_null_integrity.json",
        {
            "classification": "census",
            "candidate_population": 3 if census_mismatch else 2,
            "candidate_breakdown": {"or_zero": 1, "else_zero": 1, "get_default_zero": 0},
            "candidates": [
                {
                    "id": "rank-normalization",
                    "calculation_distortion": False,
                    "denominator_contamination": False,
                },
                {
                    "id": "growth-contribution",
                    "calculation_distortion": False,
                    "denominator_contamination": False,
                },
            ],
            "semantics": [
                {"id": "missing", "kind": "missing", "value": 0.0 if invent_zero else None},
                {"id": "real-zero", "kind": "real_zero", "value": 0.0},
            ],
            "numeric_observations": [
                {"id": "growth", "value": -100.0 if extreme_numeric else 12.5},
                {"id": "share", "value": 0.0},
            ],
            "performance_cases": [
                {
                    "id": "cause-fixture",
                    "before_calls": 1000,
                    "after_calls": 1001 if regress_performance else 900,
                    "before_ms": 100.0,
                    "after_ms": 121.0 if regress_performance else 95.0,
                }
            ],
        },
    )


def test_cause_null_integrity_gate_checks_census_semantics_and_performance(tmp_path: Path) -> None:
    result = _run(
        "cause-null-integrity",
        "--evidence",
        str(_cause_null_integrity_evidence(tmp_path)),
        "--environment",
        "unit",
    )

    assert result.returncode == 0
    assert "gate=cause_null_integrity" in result.stdout
    assert "classification=census" in result.stdout
    assert "failures=0" in result.stdout
    assert "exit_code=0" in result.stdout


def test_cause_null_integrity_gate_rejects_missing_to_zero_injection(tmp_path: Path) -> None:
    result = _run(
        "cause-null-integrity",
        "--evidence",
        str(_cause_null_integrity_evidence(tmp_path, invent_zero=True)),
        "--environment",
        "failure-injection",
    )

    assert result.returncode == 1
    assert "missing: missing value was coerced to zero" in result.stdout
    assert "exit_code=1" in result.stdout


def test_cause_null_integrity_gate_rejects_performance_injection(tmp_path: Path) -> None:
    result = _run(
        "cause-null-integrity",
        "--evidence",
        str(_cause_null_integrity_evidence(tmp_path, regress_performance=True)),
        "--environment",
        "failure-injection",
    )

    assert result.returncode == 1
    assert "cause-fixture: call count increased" in result.stdout
    assert "cause-fixture: latency ratio" in result.stdout
    assert "exit_code=1" in result.stdout


def test_cause_null_integrity_gate_rejects_candidate_census_mismatch(tmp_path: Path) -> None:
    result = _run(
        "cause-null-integrity",
        "--evidence",
        str(_cause_null_integrity_evidence(tmp_path, census_mismatch=True)),
        "--environment",
        "failure-injection",
    )

    assert result.returncode == 1
    assert "candidate census mismatch" in result.stdout
    assert "exit_code=1" in result.stdout


def test_cause_null_integrity_gate_rejects_extreme_numeric_injection(tmp_path: Path) -> None:
    result = _run(
        "cause-null-integrity",
        "--evidence",
        str(_cause_null_integrity_evidence(tmp_path, extreme_numeric=True)),
        "--environment",
        "failure-injection",
    )

    assert result.returncode == 1
    assert "growth: prohibited extreme numeric value=-100.0" in result.stdout
    assert "exit_code=1" in result.stdout


def _f116_evidence(
    tmp_path: Path,
    *,
    parent_rows: int = 0,
    truncate_storage: bool = False,
    prefer_null: bool = False,
) -> Path:
    expected_brands = [f"BRAND-{index}" for index in range(1, 10)]
    return _write_json(
        tmp_path / "f116.json",
        {
            "classification": "census",
            "specialty_observations": [
                {
                    "id": "ml_003|UBIST|sales",
                    "market_total": "100.00",
                    "specialty_total": "100.00",
                    "parent_rows": parent_rows,
                    "detail_count": 10,
                    "overcount_ratio": "1.0",
                }
            ],
            "brand_storage": [
                {
                    "id": "ml_003",
                    "expected_brands": expected_brands,
                    "stored_brands": expected_brands[:7] if truncate_storage else expected_brands,
                }
            ],
            "api_cases": [
                {
                    "id": "guardmet",
                    "returned_brands": expected_brands[:6],
                    "expected_brands": expected_brands[:6],
                    "response_bytes_before": 1200,
                    "response_bytes_after": 1200,
                }
            ],
            "canonical_cells": [
                {
                    "id": "guardmet|class",
                    "brand_value": None,
                    "product_value": "DPP-4i+MET",
                    "result_value": None if prefer_null else "DPP-4i+MET",
                    "expected_source": "product",
                }
            ],
            "performance_cases": [
                {"id": "brand-activity", "before_ms": 100.0, "after_ms": 105.0}
            ],
        },
    )


def test_f116_correctness_gate_accepts_complete_census(tmp_path: Path) -> None:
    result = check_f116_correctness(_f116_evidence(tmp_path), "unit")

    assert result.exit_code == 0
    assert result.checked == 5
    assert result.population == 5
    assert result.failures == 0


def test_f116_correctness_cli_renders_acceptance_contract(tmp_path: Path) -> None:
    result = _run(
        "f116-correctness",
        "--evidence",
        str(_f116_evidence(tmp_path)),
        "--environment",
        "unit",
    )

    assert result.returncode == 0
    assert "gate=f116_correctness" in result.stdout
    assert "classification=census" in result.stdout
    assert "checked=5" in result.stdout
    assert "population=5" in result.stdout
    assert "missing=fail" in result.stdout
    assert "exit_code=0" in result.stdout


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"parent_rows": 1}, "aggregate parent rows remain=1"),
        ({"truncate_storage": True}, "stored brand census mismatch"),
        ({"prefer_null": True}, "canonical precedence mismatch"),
    ],
)
def test_f116_correctness_gate_rejects_required_failure_injections(
    tmp_path: Path,
    kwargs: dict[str, object],
    message: str,
) -> None:
    result = check_f116_correctness(_f116_evidence(tmp_path, **kwargs), "failure-injection")

    assert result.exit_code == 1
    assert result.failures == 1
    assert any(message in detail for detail in result.details)
