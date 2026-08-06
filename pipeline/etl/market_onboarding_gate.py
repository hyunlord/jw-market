"""Fail-closed validation for append-only MI Master market onboarding."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence


REQUIRED_RECONCILIATION_CHECKS = frozenset(
    {
        "catalog_count_plus_one",
        "existing_ml_ids_unchanged",
        "existing_cd_ids_unchanged",
        "new_cd_spec_explicit",
        "all_cd_specs_bound_to_expected_identity",
        "api_registry_exposed",
    }
)


def _id_drift(
    before: Mapping[str, str],
    after: Mapping[str, str],
) -> dict[str, dict[str, str | None]]:
    return {
        identity: {
            "before": before_id,
            "after": after.get(identity),
        }
        for identity, before_id in before.items()
        if after.get(identity) != before_id
    }


def reconcile_gate_result(
    *,
    checks: Mapping[str, bool],
    spec_binding_mismatches: Sequence[Mapping[str, Any]],
    new_cd_id: str | None = None,
) -> dict[str, Any]:
    """Derive checks and final status from one authoritative mismatch list."""

    resolved_checks = dict(checks)
    missing_checks = REQUIRED_RECONCILIATION_CHECKS - resolved_checks.keys()
    if missing_checks:
        raise ValueError(
            "missing required gate checks: "
            + ", ".join(sorted(missing_checks))
        )
    mismatches = [dict(item) for item in spec_binding_mismatches]
    if mismatches:
        resolved_checks["all_cd_specs_bound_to_expected_identity"] = False
    if any(
        item.get("reason") == "missing_explicit_spec"
        and (new_cd_id is None or item.get("cd_id") == new_cd_id)
        for item in mismatches
    ):
        resolved_checks["new_cd_spec_explicit"] = False
    return {
        "checks": resolved_checks,
        "spec_binding_mismatches": mismatches,
        "passed": not mismatches and all(resolved_checks.values()),
    }


def evaluate_market_onboarding(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate a precomputed catalog/API/dashboard onboarding probe."""

    before = payload["before"]
    after = payload["after"]
    before_ml = dict(before["ml_id_by_identity"])
    after_ml = dict(after["ml_id_by_identity"])
    before_cd = dict(before["cd_id_by_identity"])
    after_cd = dict(after["cd_id_by_identity"])
    new_ml_id = str(payload["new_ml_id"])
    new_cd_id = str(payload["new_cd_id"])
    before_ml_ids = set(before_ml.values())
    after_ml_ids = set(after_ml.values())
    before_cd_ids = set(before_cd.values())
    after_cd_ids = set(after_cd.values())

    ml_id_drift = _id_drift(before_ml, after_ml)
    cd_id_drift = _id_drift(before_cd, after_cd)
    explicit_spec_ids = {str(value) for value in payload["explicit_spec_ids"]}
    mismatches = [dict(item) for item in payload["spec_binding_mismatches"]]
    if new_cd_id not in explicit_spec_ids and not any(
        item.get("cd_id") == new_cd_id
        and item.get("reason") == "missing_explicit_spec"
        for item in mismatches
    ):
        mismatches.append(
            {"cd_id": new_cd_id, "reason": "missing_explicit_spec"}
        )

    parent_members = {str(value) for value in payload["parent_member_ids"]}
    cd_members = {str(value) for value in payload["cd_member_ids"]}
    if parent_members and cd_members == parent_members:
        parent_cd_relation = "same"
    elif cd_members and cd_members < parent_members:
        parent_cd_relation = "subset"
    else:
        parent_cd_relation = "invalid"

    checks = {
        "catalog_count_plus_one": (
            len(after_ml_ids) == len(before_ml_ids) + 1
            and len(after_cd_ids) == len(before_cd_ids) + 1
            and len(after_ml_ids) == len(after_ml)
            and len(after_cd_ids) == len(after_cd)
        ),
        "existing_ml_ids_unchanged": not ml_id_drift,
        "existing_cd_ids_unchanged": not cd_id_drift,
        "new_ml_id_present": (
            new_ml_id not in before_ml_ids and new_ml_id in after_ml_ids
        ),
        "new_cd_id_present": (
            new_cd_id not in before_cd_ids and new_cd_id in after_cd_ids
        ),
        "new_cd_spec_explicit": new_cd_id in explicit_spec_ids,
        "all_cd_specs_bound_to_expected_identity": not mismatches,
        "parent_cd_row_relation_valid": parent_cd_relation in {"same", "subset"},
        "api_registry_exposed": new_cd_id
        in {str(value) for value in payload["api_registry_cd_ids"]},
        "dashboard_marker_present": (
            new_ml_id in {str(value) for value in payload["dashboard_markers"]}
            and new_cd_id
            in {str(value) for value in payload["dashboard_markers"]}
        ),
    }
    reconciled = reconcile_gate_result(
        checks=checks,
        spec_binding_mismatches=mismatches,
        new_cd_id=new_cd_id,
    )
    return {
        **reconciled,
        "new_ml_id": new_ml_id,
        "new_cd_id": new_cd_id,
        "ml_id_drift": ml_id_drift,
        "cd_id_drift": cd_id_drift,
        "parent_cd_relation": parent_cd_relation,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if len(args) != 1:
        print("usage: python -m pipeline.etl.market_onboarding_gate INPUT.json")
        return 2
    payload = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    if "before" not in payload or "after" not in payload:
        raise ValueError(
            "executable gate requires a full onboarding probe with "
            "before and after catalogs"
        )
    result = evaluate_market_onboarding(payload)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
