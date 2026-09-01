from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field


class ReleaseGateDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    release_allowed: bool
    blocking_cases: tuple[str, ...]
    reason_codes: tuple[str, ...]


class ShadowActivationObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    eligible_tools_count: int = Field(ge=0)
    forbidden_tool_calls: int = Field(default=0, ge=0)
    invalid_argument_calls: int = Field(default=0, ge=0)
    wrong_source_owner_calls: int = Field(default=0, ge=0)
    normal_to_typed_unsupported: int = Field(default=0, ge=0)


class ShadowActivationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enforce_allowed: bool
    checked: int
    eligible_cases: int
    blocking_conditions: tuple[str, ...]


def evaluate_release_gate(manifest: Mapping[str, Any]) -> ReleaseGateDecision:
    blocking: list[str] = []
    reasons: list[str] = []
    for case in manifest.get("cases") or ():
        if not isinstance(case, Mapping) or case.get("release_gate") != "BLOCKED":
            continue
        blocking.append(str(case.get("case_id") or "UNKNOWN"))
        reasons.append(
            "APPROVED_ORACLE_MISSING"
            if case.get("required_oracle_before_activation")
            else "RELEASE_CASE_BLOCKED"
        )
    return ReleaseGateDecision(
        release_allowed=not blocking,
        blocking_cases=tuple(blocking),
        reason_codes=tuple(reasons),
    )


def evaluate_shadow_activation_gate(
    observations: Sequence[ShadowActivationObservation],
) -> ShadowActivationDecision:
    checked = len(observations)
    eligible_cases = sum(item.eligible_tools_count > 0 for item in observations)
    conditions: list[str] = []
    if checked == 0:
        conditions.append("EMPTY_SHADOW_POPULATION")
    if eligible_cases == 0:
        conditions.append("NO_ELIGIBLE_TOOL_CASES")
    checks = (
        ("FORBIDDEN_TOOL_CALLS", "forbidden_tool_calls"),
        ("INVALID_TOOL_ARGUMENTS", "invalid_argument_calls"),
        ("WRONG_SOURCE_OWNER", "wrong_source_owner_calls"),
        ("NORMAL_TO_TYPED_UNSUPPORTED", "normal_to_typed_unsupported"),
    )
    for reason, field_name in checks:
        if any(getattr(item, field_name) > 0 for item in observations):
            conditions.append(reason)
    return ShadowActivationDecision(
        enforce_allowed=not conditions,
        checked=checked,
        eligible_cases=eligible_cases,
        blocking_conditions=tuple(conditions),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail closed on blocked external-tool routing v4 cases.")
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args(argv)
    decision = evaluate_release_gate(json.loads(args.manifest.read_text(encoding="utf-8")))
    print(decision.model_dump_json())
    return 0 if decision.release_allowed else 2


if __name__ == "__main__":
    raise SystemExit(main())
