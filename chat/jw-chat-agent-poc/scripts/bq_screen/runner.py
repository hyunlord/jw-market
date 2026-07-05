from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from scripts.bq_screen.models import BqCase, BqScreenInput, Finding, ScreenResult
from scripts.bq_screen.rules import screen_answer


def screen_capture_dir(capture_dir: Path) -> list[ScreenResult]:
    raw_dir = capture_dir / "screen"
    if not raw_dir.is_dir():
        raise FileNotFoundError(f"screen raw directory not found: {raw_dir}")
    results: list[ScreenResult] = []
    for raw_path in sorted(raw_dir.glob("*.raw.json")):
        results.append(screen_answer(load_raw_capture(raw_path)))
    return results


def load_raw_capture(raw_path: Path) -> BqScreenInput:
    data = json.loads(raw_path.read_text(encoding="utf-8"))
    case_data = _mapping(data.get("case"))
    response = _mapping(data.get("response"))
    trace = _mapping(response.get("trace"))
    tools = tuple(str(item) for item in _list(trace.get("tools_called")))
    return BqScreenInput(
        case=BqCase(
            id=str(case_data.get("id") or raw_path.stem.removesuffix(".raw")),
            brand=str(case_data.get("brand") or ""),
            type=str(case_data.get("type") or ""),
            question=str(case_data.get("question") or ""),
            cohort=str(case_data.get("cohort") or ""),
            source=str(case_data.get("source") or ""),
        ),
        status=_optional_int(data.get("status")),
        elapsed_s=_optional_float(data.get("elapsed_s")),
        error=_optional_str(data.get("error")),
        text=str(response.get("text") or ""),
        tools=tools,
    )


def result_payload(results: list[ScreenResult]) -> dict[str, Any]:
    return {
        "case_count": len(results),
        "flagged_count": sum(1 for result in results if result.flags),
        "confirm_needed_count": sum(1 for result in results if result.confirm_needed),
        "results": [_result_dict(result) for result in results],
        "flag_counts": _count_flags(results, "flags"),
        "confirm_needed_counts": _count_flags(results, "confirm_needed"),
    }


def write_outputs(results: list[ScreenResult], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result_payload(results), ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Screen BQ capture answers for known format and semantic defects.")
    parser.add_argument("--capture-dir", required=True, type=Path, help="BQ capture directory containing screen/*.raw.json")
    parser.add_argument("--out", required=True, type=Path, help="Output JSON path")
    args = parser.parse_args(argv)
    results = screen_capture_dir(args.capture_dir)
    write_outputs(results, args.out)
    print(json.dumps(result_payload(results), ensure_ascii=False))
    return 0


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _result_dict(result: ScreenResult) -> dict[str, Any]:
    return {
        "case_id": result.case_id,
        "flags": list(result.flags),
        "confirm_needed": list(result.confirm_needed),
        "findings": [_finding_dict(finding) for finding in result.findings],
    }


def _finding_dict(finding: Finding) -> dict[str, str]:
    return asdict(finding)


def _count_flags(results: list[ScreenResult], attr: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for result in results:
        for flag in getattr(result, attr):
            counts[flag] = counts.get(flag, 0) + 1
    return dict(sorted(counts.items()))


if __name__ == "__main__":
    raise SystemExit(main())
