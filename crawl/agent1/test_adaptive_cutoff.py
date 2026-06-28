#!/usr/bin/env python3
"""Small executable tests for the adaptive cutoff helper."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from adaptive_cutoff import adaptive_cutoff


def make_events(scores: list[int]) -> list[dict[str, int]]:
    return [{"event_id": index, "score": score} for index, score in enumerate(scores)]


def run_tests() -> dict[str, object]:
    cases: list[dict[str, object]] = []

    filtered, cutoff = adaptive_cutoff([], target_min=10, target_max=50, init_cutoff=35)
    cases.append(
        {
            "name": "empty_list",
            "passed": filtered == [] and cutoff == 35,
            "returned_count": len(filtered),
            "applied_cutoff": cutoff,
        }
    )

    filtered, cutoff = adaptive_cutoff(
        make_events(list(range(101))),
        target_min=1,
        target_max=10,
        init_cutoff=50,
    )
    cases.append(
        {
            "name": "target_max_overflow",
            "passed": len(filtered) <= 10 and min(e["score"] for e in filtered) >= cutoff,
            "returned_count": len(filtered),
            "applied_cutoff": cutoff,
        }
    )

    filtered, cutoff = adaptive_cutoff(
        make_events([5, 10, 15, 20, 25]),
        target_min=3,
        target_max=15,
        init_cutoff=50,
    )
    cases.append(
        {
            "name": "target_min_underflow",
            "passed": len(filtered) >= 3 and cutoff < 50,
            "returned_count": len(filtered),
            "applied_cutoff": cutoff,
        }
    )

    filtered, cutoff = adaptive_cutoff(
        make_events([score % 101 for score in range(1000)]),
        target_min=10,
        target_max=50,
        init_cutoff=35,
    )
    cases.append(
        {
            "name": "panel_spec_1000_events",
            "passed": len(filtered) == 50,
            "returned_count": len(filtered),
            "applied_cutoff": cutoff,
        }
    )

    filtered, cutoff = adaptive_cutoff(
        make_events([10, 15, 30, 45, 55, 60, 70, 74, 75, 76, 80, 82, 85, 88, 90, 91, 92, 94, 96, 98, 99, 40, 35, 25, 65, 67, 73, 79, 83, 87]),
        target_min=3,
        target_max=15,
        init_cutoff=75,
    )
    cases.append(
        {
            "name": "marker_spec_30_events",
            "passed": 3 <= len(filtered) <= 15,
            "returned_count": len(filtered),
            "applied_cutoff": cutoff,
        }
    )

    return {"test_cases": cases, "all_passed": all(case["passed"] for case in cases)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run_tests()
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text)
    return 0 if result["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

