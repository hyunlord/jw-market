from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys


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


def test_golden_gate_requires_every_expected_identity_and_exact_hash(tmp_path: Path) -> None:
    contracts = []
    observations = []
    for index in range(4):
        identifier = f"endpoint-{index}"
        expected_payload = {"value": index}
        observed_payload = expected_payload if index < 3 else {"value": 999}
        contracts.append(
            {
                "id": identifier,
                "canonical_sha256": _canonical_sha(expected_payload),
                "request": {"method": "GET", "path": f"/api/{identifier}"},
                "truth_basis": "fixture truth",
                "measured_at": "2026-07-14T00:00:00+09:00",
                "database": "fixture",
                "runtime_provenance": "sha256:fixture",
            }
        )
        observations.append({"id": identifier, "payload": observed_payload})

    result = _run(
        "goldens",
        "--contracts",
        str(_write_json(tmp_path / "contracts.json", {"contracts": contracts})),
        "--observations",
        str(_write_json(tmp_path / "observations.json", observations)),
        "--environment",
        "failure-injection",
    )

    assert result.returncode == 1
    assert "endpoint-3" in result.stdout
    assert "gate=api_goldens" in result.stdout
    assert "checked=4" in result.stdout
    assert "population=4" in result.stdout
    assert "failures=1" in result.stdout
    assert "exit_code=1" in result.stdout


def test_golden_gate_rejects_missing_endpoint_identity(tmp_path: Path) -> None:
    payload = {"ok": True}
    contract = {
        "id": "required",
        "canonical_sha256": _canonical_sha(payload),
        "request": {"method": "GET", "path": "/required"},
        "truth_basis": "fixture truth",
        "measured_at": "2026-07-14T00:00:00+09:00",
        "database": "fixture",
        "runtime_provenance": "sha256:fixture",
    }
    result = _run(
        "goldens",
        "--contracts",
        str(_write_json(tmp_path / "contracts.json", {"contracts": [contract]})),
        "--observations",
        str(_write_json(tmp_path / "observations.json", [])),
    )

    assert result.returncode == 1
    assert "missing identities: required" in result.stdout
    assert "checked=0" in result.stdout
    assert "population=1" in result.stdout


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
    for contract in contracts:
        assert len(contract["canonical_sha256"]) == 64
        assert contract["request"]["method"] in {"GET", "POST"}
        assert contract["request"]["path"].startswith("/jw-market-backend-api/api/")
        assert contract["truth_basis"]
        assert contract["measured_at"]
        assert contract["database"] == "jw_mart_d2_stage_20260630_r2"
        assert contract["runtime_provenance"]


def test_golden_gate_passes_only_when_all_contracts_match(tmp_path: Path) -> None:
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
    observations = [
        {"id": identifier, "payload": payload} for identifier, payload in payloads.items()
    ]

    result = _run(
        "goldens",
        "--contracts",
        str(_write_json(tmp_path / "contracts.json", {"contracts": contracts})),
        "--observations",
        str(_write_json(tmp_path / "observations.json", observations)),
    )

    assert result.returncode == 0
    assert "checked=2" in result.stdout
    assert "population=2" in result.stdout
    assert "failures=0" in result.stdout
    assert "exit_code=0" in result.stdout


def test_segment_sum_sample_requires_every_expected_level(tmp_path: Path) -> None:
    expected = _write_json(
        tmp_path / "expected.json",
        {
            "classification": "sample",
            "identities": [
                {
                    "market": "C10A1",
                    "period": "2026-05",
                    "source": "ubist",
                    "measure": "sales",
                    "level": level,
                }
                for level in ("form", "molecule")
            ],
        },
    )
    observations = _write_json(
        tmp_path / "observations.json",
        [
            {
                "market": "C10A1",
                "period": "2026-05",
                "source": "ubist",
                "measure": "sales",
                "level": "form",
                "segment_sum": 100.0,
                "market_total": 100.0,
            }
        ],
    )

    result = _run(
        "segment-sum",
        "--expected-identities",
        str(expected),
        "--observations",
        str(observations),
        "--abs-tol",
        "0.01",
    )

    assert result.returncode == 1
    assert "missing identities:" in result.stdout
    assert "gate=segment_sum" in result.stdout
    assert "classification=sample" in result.stdout
    assert "checked=1" in result.stdout
    assert "population=2" in result.stdout


def test_segment_sum_uses_absolute_tolerance_without_hiding_missing_levels(tmp_path: Path) -> None:
    identity = {
        "market": "C10A1",
        "period": "2026-05",
        "source": "ubist",
        "measure": "sales",
        "level": "form",
    }
    expected = _write_json(
        tmp_path / "expected.json",
        {"classification": "sample", "identities": [identity]},
    )
    observations = _write_json(
        tmp_path / "observations.json",
        [{**identity, "segment_sum": 100.009, "market_total": 100.0}],
    )

    result = _run(
        "segment-sum",
        "--expected-identities",
        str(expected),
        "--observations",
        str(observations),
        "--abs-tol",
        "0.01",
    )

    assert result.returncode == 0
    assert "tolerance=abs_tol=0.01,rel_tol=0" in result.stdout
    assert "failures=0" in result.stdout


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
    result = _run(
        "brand-sources",
        "--expectations",
        str(_write_json(tmp_path / "expectations.json", _brand_source_expectations())),
        "--observations",
        str(_write_json(tmp_path / "observations.json", _brand_source_observations())),
        "--environment",
        "fixture",
    )

    assert result.returncode == 0
    assert "gate=brand_sources" in result.stdout
    assert "classification=census" in result.stdout
    assert "checked=8" in result.stdout
    assert "population=8" in result.stdout
    assert "failures=0" in result.stdout


def test_brand_sources_gate_failure_injection_exits_one(tmp_path: Path) -> None:
    observations = _brand_source_observations()
    observations[0]["listed"] = not observations[0]["has_data"]
    result = _run(
        "brand-sources",
        "--expectations",
        str(_write_json(tmp_path / "expectations.json", _brand_source_expectations())),
        "--observations",
        str(_write_json(tmp_path / "observations.json", observations)),
        "--environment",
        "failure-injection",
    )

    assert result.returncode == 1
    assert "listed/data mismatch" in result.stdout
    assert "failures=1" in result.stdout
    assert "exit_code=1" in result.stdout


def test_brand_sources_gate_rejects_empty_or_incomplete_population(tmp_path: Path) -> None:
    result = _run(
        "brand-sources",
        "--expectations",
        str(_write_json(tmp_path / "expectations.json", _brand_source_expectations())),
        "--observations",
        str(_write_json(tmp_path / "observations.json", [])),
    )

    assert result.returncode == 1
    assert "empty observation population is a failure" in result.stdout
    assert "checked=0" in result.stdout
    assert "population=8" in result.stdout
