from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.etl.market_onboarding_gate import (
    evaluate_market_onboarding,
    main,
    reconcile_gate_result,
)


def _valid_input() -> dict[str, object]:
    return {
        "before": {
            "ml_id_by_identity": {"시장A": "ml_001"},
            "cd_id_by_identity": {"제품A": "cd_001"},
        },
        "after": {
            "ml_id_by_identity": {"시장A": "ml_001", "신규시장": "ml_002"},
            "cd_id_by_identity": {"제품A": "cd_001", "신규제품": "cd_002"},
        },
        "new_ml_id": "ml_002",
        "new_cd_id": "cd_002",
        "explicit_spec_ids": ["cd_001", "cd_002"],
        "spec_binding_mismatches": [],
        "parent_member_ids": ["brand_a", "brand_b"],
        "cd_member_ids": ["brand_a"],
        "api_registry_cd_ids": ["cd_001", "cd_002"],
        "dashboard_markers": ["ml_002", "cd_002"],
    }


def test_prior_stale_mismatch_input_is_reconciled_to_failure() -> None:
    fixture = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "market_onboarding"
        / "prior_gate_declared_spec.json"
    )
    prior = json.loads(fixture.read_text(encoding="utf-8"))

    result = reconcile_gate_result(
        checks=prior["checks"],
        spec_binding_mismatches=prior["spec_binding_mismatches"],
        new_cd_id=prior["new_cd_id"],
    )

    assert prior["passed"] is True
    assert result["passed"] is False
    assert result["checks"]["new_cd_spec_explicit"] is False
    assert result["checks"]["all_cd_specs_bound_to_expected_identity"] is False


def _complete_reconciliation_checks() -> dict[str, bool]:
    return {
        "catalog_count_plus_one": True,
        "existing_ml_ids_unchanged": True,
        "existing_cd_ids_unchanged": True,
        "new_cd_spec_explicit": True,
        "all_cd_specs_bound_to_expected_identity": True,
        "api_registry_exposed": True,
    }


def test_empty_mismatch_input_remains_green() -> None:
    result = reconcile_gate_result(
        checks=_complete_reconciliation_checks(),
        spec_binding_mismatches=[],
    )

    assert result["passed"] is True


@pytest.mark.parametrize("checks", [{}, {"catalog_count_plus_one": True}])
def test_incomplete_reconciliation_check_set_is_rejected(
    checks: dict[str, bool],
) -> None:
    with pytest.raises(ValueError, match="missing required gate checks"):
        reconcile_gate_result(
            checks=checks,
            spec_binding_mismatches=[],
        )


def test_cli_rejects_archived_reconciliation_payload(capsys) -> None:
    fixture = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "market_onboarding"
        / "prior_gate_declared_spec.json"
    )

    with pytest.raises(ValueError, match="full onboarding probe"):
        main([str(fixture)])
    assert capsys.readouterr().out == ""


def test_cli_accepts_full_onboarding_probe(tmp_path: Path, capsys) -> None:
    fixture = tmp_path / "onboarding.json"
    fixture.write_text(
        json.dumps(_valid_input(), ensure_ascii=False),
        encoding="utf-8",
    )

    exit_code = main([str(fixture)])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["passed"] is True


def test_append_only_market_addition_passes() -> None:
    result = evaluate_market_onboarding(_valid_input())

    assert result["passed"] is True
    assert result["ml_id_drift"] == {}
    assert result["cd_id_drift"] == {}
    assert result["parent_cd_relation"] == "subset"


def test_middle_insertion_with_id_drift_fails() -> None:
    payload = _valid_input()
    payload["after"] = {
        "ml_id_by_identity": {"신규시장": "ml_001", "시장A": "ml_002"},
        "cd_id_by_identity": {"신규제품": "cd_001", "제품A": "cd_002"},
    }

    result = evaluate_market_onboarding(payload)

    assert result["passed"] is False
    assert result["checks"]["existing_ml_ids_unchanged"] is False
    assert result["checks"]["existing_cd_ids_unchanged"] is False
    assert result["checks"]["new_cd_spec_explicit"] is True


def test_new_identity_reusing_existing_ids_fails() -> None:
    payload = _valid_input()
    payload["after"] = {
        "ml_id_by_identity": {"시장A": "ml_001", "신규시장": "ml_001"},
        "cd_id_by_identity": {"제품A": "cd_001", "신규제품": "cd_001"},
    }
    payload["new_ml_id"] = "ml_001"
    payload["new_cd_id"] = "cd_001"

    result = evaluate_market_onboarding(payload)

    assert result["passed"] is False
    assert result["checks"]["catalog_count_plus_one"] is False
    assert result["checks"]["new_ml_id_present"] is False
    assert result["checks"]["new_cd_id_present"] is False


def test_missing_explicit_spec_fails() -> None:
    payload = _valid_input()
    payload["explicit_spec_ids"] = ["cd_001"]

    result = evaluate_market_onboarding(payload)

    assert result["passed"] is False
    assert result["checks"]["new_cd_spec_explicit"] is False
    assert {item["reason"] for item in result["spec_binding_mismatches"]} == {
        "missing_explicit_spec"
    }


def test_identity_mismatch_fails_even_when_spec_id_exists() -> None:
    payload = _valid_input()
    payload["spec_binding_mismatches"] = [
        {
            "cd_id": "cd_002",
            "reason": "identity_mismatch",
            "differences": {
                "strategic_market_id": {
                    "expected": "strategy_002",
                    "actual": "strategy_001",
                }
            },
        }
    ]

    result = evaluate_market_onboarding(payload)

    assert result["passed"] is False
    assert result["checks"]["new_cd_spec_explicit"] is True
    assert result["checks"]["all_cd_specs_bound_to_expected_identity"] is False
