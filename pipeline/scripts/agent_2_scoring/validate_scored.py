#!/usr/bin/env python3
"""Validate Phase beta/delta _scored JSON files and source markers."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


REQUIRED_TOP = {"matches", "summary", "tag"}
REQUIRED_MATCH = {"drug", "score", "reason"}
VALID_TAGS = {"신약/R&D", "정책/규제", "공급/생산", "자본/경영", "외부/트렌드", "기타"}


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_scored(path: Path) -> list[str]:
    errors: list[str] = []
    obj = load(path)
    missing = REQUIRED_TOP - set(obj)
    if missing:
        errors.append(f"missing top-level keys: {sorted(missing)}")
    if obj.get("tag") not in VALID_TAGS:
        errors.append(f"invalid tag: {obj.get('tag')}")
    if not isinstance(obj.get("matches"), list):
        errors.append("matches is not a list")
        return errors
    for idx, match in enumerate(obj["matches"]):
        if not isinstance(match, dict):
            errors.append(f"matches[{idx}] is not an object")
            continue
        missing_match = REQUIRED_MATCH - set(match)
        if missing_match:
            errors.append(f"matches[{idx}] missing keys: {sorted(missing_match)}")
        if not isinstance(match.get("drug"), str) or not match.get("drug").strip():
            errors.append(f"matches[{idx}] drug is empty")
        score = match.get("score")
        if not isinstance(score, (int, float)) or not 0 <= score <= 100:
            errors.append(f"matches[{idx}] score out of range: {score}")
        if not isinstance(match.get("reason"), str) or not match.get("reason").strip():
            errors.append(f"matches[{idx}] reason is empty")
    return errors


def validate_markers(output_root: Path) -> list[str]:
    errors: list[str] = []
    for path in output_root.glob("*/news_5years_*/*.json"):
        obj = load(path)
        if obj.get("scored") is True:
            score_file = obj.get("score_file")
            if not score_file:
                errors.append(f"{path}: scored=true but score_file missing")
                continue
            if not (output_root / score_file).exists():
                errors.append(f"{path}: missing score_file {score_file}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", type=Path, help="Output root or scored directory.")
    parser.add_argument("--output-root", type=Path, help="Output root containing _scored, or a scored directory.")
    args = parser.parse_args()
    target = (args.output_root or args.path)
    if target is None:
        parser.error("path or --output-root is required")
    output_root = target.resolve()
    if output_root.name == "_scored" or any(p.name.endswith("_scored.json") for p in output_root.glob("*_scored.json")):
        scored_files = sorted(output_root.glob("**/*_scored.json"))
        marker_root = output_root.parent
    else:
        scored_files = sorted(output_root.glob("_scored/**/*_scored.json"))
        marker_root = output_root
    errors: list[dict[str, str]] = []
    for path in scored_files:
        for error in validate_scored(path):
            errors.append({"file": str(path), "error": error})
    for error in validate_markers(marker_root):
        errors.append({"file": str(output_root), "error": error})
    result = {"checked_scored": len(scored_files), "errors": errors}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
