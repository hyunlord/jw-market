from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path

from pipeline.scripts.analysis.brand_activity.auto_topic.models import JsonValue


def main() -> None:
    """Run the local static-quality gate for auto_topic scripts."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = inspect_package(Path(args.path))
    output = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    print(output if args.json else output)
    if result["docstring_coverage"] < 1.0 or result["dead_code_count"] != 0 or result["rationale_comment_status"] != "PASS":
        raise SystemExit(1)


def inspect_package(path: Path) -> dict[str, JsonValue]:
    """Inspect function docstrings, unused top-level functions, and rationale markers."""
    files = sorted(file for file in path.glob("*.py") if file.name != "__init__.py")
    functions: list[tuple[str, str]] = []
    called_names: set[str] = set()
    rationale_markers = 0
    for file in files:
        source = file.read_text(encoding="utf-8")
        rationale_markers += source.count("Rationale:")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                called_names.add(node.func.id)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                called_names.add(node.func.attr)
        for node in tree.body:
            if isinstance(node, ast.FunctionDef):
                functions.append((file.name, node.name))
    missing = _missing_docstrings(files)
    dead = _dead_functions(functions, called_names)
    return {
        "file_count": len(files),
        "function_count": len(functions),
        "missing_docstrings": missing,
        "docstring_coverage": round((len(functions) - len(missing)) / len(functions), 3) if functions else 1.0,
        "dead_functions": dead,
        "dead_code_count": len(dead),
        "rationale_comment_status": "PASS" if rationale_markers >= 3 else "FAIL",
    }


def _missing_docstrings(files: list[Path]) -> list[str]:
    """Return function names that lack docstrings."""
    missing: list[str] = []
    for file in files:
        tree = ast.parse(file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and ast.get_docstring(node) is None:
                missing.append(f"{file.name}:{node.name}")
    return missing


def _dead_functions(functions: list[tuple[str, str]], called_names: set[str]) -> list[str]:
    """Return top-level helper names that are neither entrypoints nor referenced."""
    allow = {"main"}
    exported_prefixes = ("render_", "build_", "fetch_", "load_", "write_", "create_", "expected_", "scope_", "quality_", "execute_", "skipped_", "report_", "raw_", "file_", "generated_", "resolve_", "connect_", "read_", "market_", "brand_", "large_", "choose_", "deterministic_", "stable_", "stabilize_", "axis_", "max_", "share_", "mechanical_", "drift_", "dictionary_", "competitor_", "call_", "parse_", "normalize_", "topics_", "prompt_", "text_", "estimate_", "redacted_", "inspect_", "group_")
    dead: list[str] = []
    for file_name, function_name in functions:
        if function_name in allow or function_name.startswith(exported_prefixes) or function_name in called_names:
            continue
        if function_name.startswith("_") and function_name in called_names:
            continue
        if function_name.startswith("_") and function_name not in called_names:
            dead.append(f"{file_name}:{function_name}")
    return dead


if __name__ == "__main__":
    main()
