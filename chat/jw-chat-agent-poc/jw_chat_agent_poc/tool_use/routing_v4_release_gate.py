from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from pydantic import BaseModel, ConfigDict


class ReleaseGateDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    release_allowed: bool
    blocking_cases: tuple[str, ...]
    reason_codes: tuple[str, ...]


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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail closed on blocked external-tool routing v4 cases.")
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args(argv)
    decision = evaluate_release_gate(json.loads(args.manifest.read_text(encoding="utf-8")))
    print(decision.model_dump_json())
    return 0 if decision.release_allowed else 2


if __name__ == "__main__":
    raise SystemExit(main())
