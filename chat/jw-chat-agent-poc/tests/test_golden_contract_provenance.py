from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from scripts.golden_contract_gate import numeric_equal, validate_contracts, validate_registry


GOLDEN_ROOT = Path(__file__).parent / "goldens"


def _contract() -> dict:
    return {
        "id": "injected_contract",
        "gate_enabled": True,
        "request": {"kind": "calculation", "exact": "SUM(source.amount)"},
        "generation_method": {
            "description": "Sum the independently extracted source column.",
            "canonicalization": "Decimal integer comparison",
            "environment": "local-test",
        },
        "expected": {"value": 10},
        "truth_basis_status": "confirmed",
        "truth_basis": {
            "type": "source_reproduction",
            "evidence": "Original source rows were independently summed.",
            "evidence_paths": ["evidence/audit.txt"],
            "independent_of_observation": True,
        },
        "measurement_context": {
            "measured_at": "2026-07-15T00:00:00+09:00",
            "database": "not_applicable: source-file calculation",
            "build_sha": "64ce6f56242a8f924cd62028e687087a670b9c69",
            "runtime_digest": "not_applicable: source-file calculation",
            "file_sha256": "a" * 64,
        },
    }


def _validate(contract: dict):
    return validate_contracts(
        [contract],
        registry_path=Path("tests/goldens/injected.json"),
        environment="failure-injection",
    )


def test_i1_truth_basis_missing_fails() -> None:
    contract = _contract()
    contract.pop("truth_basis")

    result = _validate(contract)

    assert result.exit_code == 1
    assert any("truth_basis" in failure for failure in result.failures)


def test_i2_mock_fixture_cannot_be_truth_basis() -> None:
    contract = _contract()
    contract["truth_basis"] = {
        "type": "mock_fixture",
        "evidence": "Expected value copied from a monkeypatched result.",
        "evidence_paths": ["tests/test_renderer.py"],
        "independent_of_observation": False,
    }

    result = _validate(contract)

    assert result.exit_code == 1
    assert any("mock" in failure for failure in result.failures)


def test_i3_tmp_input_cannot_supply_a_golden() -> None:
    contract = _contract()
    contract["truth_basis"]["evidence_paths"] = ["/tmp/transient-golden.json"]

    result = _validate(contract)

    assert result.exit_code == 1
    assert any("temporary path" in failure for failure in result.failures)


def test_i4_numeric_comparison_rejects_partial_token_match() -> None:
    assert numeric_equal("29.52", "29.52") is True
    assert numeric_equal("29.52", "29.53") is False
    assert numeric_equal("29.5", "29.53") is False


def test_i5_snapshot_rehash_cannot_validate_itself() -> None:
    contract = _contract()
    contract["truth_basis"] = {
        "type": "snapshot_rehash",
        "evidence": "Rehash the same stored response used as expected.",
        "evidence_paths": ["evidence/stale-response.json"],
        "independent_of_observation": False,
    }

    result = _validate(contract)

    assert result.exit_code == 1
    assert any("self-reference" in failure for failure in result.failures)


def test_i6_zero_population_fails() -> None:
    result = validate_contracts(
        [],
        registry_path=Path("tests/goldens/empty.json"),
        environment="failure-injection",
    )

    assert result.population == 0
    assert result.exit_code == 1


def test_g4_tracked_registry_is_complete() -> None:
    result = validate_registry(GOLDEN_ROOT, environment="local-test")

    assert result.classification == "census"
    assert result.checked == result.population
    assert result.population > 0
    assert result.failures == ()
    assert result.exit_code == 0


def test_acceptance_output_separates_checked_from_population() -> None:
    result = validate_registry(GOLDEN_ROOT, environment="local-test")

    assert result.as_acceptance() == {
        "gate": "golden_truth_basis",
        "classification": "census",
        "checked": 10,
        "population": 10,
        "missing": "fail",
        "tolerance": "exact",
        "failures": 0,
        "exit_code": 0,
        "environment": "local-test",
    }


def test_p91_contract_is_relational_not_a_fixed_digest() -> None:
    payload = "\n".join(
        path.read_text(encoding="utf-8")
        for path in GOLDEN_ROOT.glob("*.json")
    )
    retired_digest = "6e2490105bf5d659714b2c694966315d2" + "d23cfe6643e33d3605e63618eef5954"

    assert retired_digest not in payload
    assert "PSM3" in payload
    assert "PSM4" in payload
    assert "byte-identical" in payload


def test_contract_values_remain_unchanged() -> None:
    documents = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in GOLDEN_ROOT.glob("*.json")
    ]
    serialized = json.dumps(documents, ensure_ascii=False, separators=(",", ":"))

    for value in (
        "386933825518",
        "21978584141",
        "15188575523",
        "6790008618",
        "3853883875",
        "3315233364",
        "2679529",
        "2555501",
        "29.52",
        "253.62",
    ):
        assert value in serialized
