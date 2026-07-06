#!/usr/bin/env python3
"""Compare the dynamic matrix display shape against the cached cause shape.

This intentionally checks only the portal-critical matrix display arrays added
to the v0.9.67 gate. Broader optional sections are left to follow-up tracks.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


MATRIX_PATHS = (
    ("data", "ei_ms_matrix"),
    ("data", "growth_contribution_ms_matrix"),
)


def _load_payload(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if isinstance(payload, dict) and isinstance(payload.get("result"), dict):
        return payload["result"]
    if isinstance(payload, dict):
        return payload
    raise ValueError(f"{path} does not contain a JSON object")


def _get_path(payload: dict[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = payload
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def _row_keyset(section: Any) -> list[str]:
    if not isinstance(section, dict) or not isinstance(section.get("data"), list) or not section["data"]:
        return []
    first = section["data"][0]
    return sorted(first) if isinstance(first, dict) else []


def _section_shape(section: Any) -> dict[str, Any]:
    if not isinstance(section, dict):
        return {"type": type(section).__name__}
    data = section.get("data")
    return {
        "type": "dict",
        "keys": sorted(section),
        "data_type": type(data).__name__,
        "data_len": len(data) if isinstance(data, list) else None,
        "row_keys": _row_keyset(section),
        "has_ms_avg_pct": "ms_avg_pct" in section,
    }


def compare(prod: dict[str, Any], dynamic: dict[str, Any]) -> dict[str, Any]:
    comparisons = {}
    failures = []
    for path in MATRIX_PATHS:
        label = ".".join(path)
        prod_shape = _section_shape(_get_path(prod, path))
        dynamic_shape = _section_shape(_get_path(dynamic, path))
        matches = prod_shape == dynamic_shape
        comparisons[label] = {
            "prod": prod_shape,
            "dynamic": dynamic_shape,
            "matches": matches,
        }
        if not matches:
            failures.append(label)
    return {"ok": not failures, "failures": failures, "comparisons": comparisons}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prod", required=True, type=Path, help="Cached /api/cause JSON capture")
    parser.add_argument("--dynamic", required=True, type=Path, help="Dynamic endpoint JSON capture")
    parser.add_argument("--output", type=Path, help="Optional JSON result path")
    args = parser.parse_args()

    result = compare(_load_payload(args.prod), _load_payload(args.dynamic))
    text = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
