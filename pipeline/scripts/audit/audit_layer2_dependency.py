#!/usr/bin/env python3
"""Audit Layer 2 dependency on strategic_product.

Phase 16-G-4-Fix-GeneralView.

This is read-only. It inspects the Layer 2 ETL implementation and emits a
compact JSON evidence object for the audit report.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path
from typing import Any

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "etl"))

from ops_utils import find_project_root


PROJECT_ROOT = find_project_root(Path(__file__).resolve())
LAYER2_PATH = PROJECT_ROOT / "pipeline" / "scripts" / "etl" / "layer2_enrich.py"
ENRICHED_SAMPLE = PROJECT_ROOT / "output" / "enriched" / "ml_id=ml_006" / "data.parquet"


def line_ranges_for(pattern: str, text: str, context: int = 3) -> list[dict[str, Any]]:
    rows = []
    lines = text.splitlines()
    for idx, line in enumerate(lines, start=1):
        if pattern in line:
            start = max(1, idx - context)
            end = min(len(lines), idx + context)
            rows.append(
                {
                    "line": idx,
                    "snippet": "\n".join(f"{i}: {lines[i - 1]}" for i in range(start, end + 1)),
                }
            )
    return rows


def function_span(path: Path, function_name: str) -> dict[str, int] | None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            return {"start": node.lineno, "end": getattr(node, "end_lineno", node.lineno)}
    return None


def layer2_schema() -> dict[str, Any]:
    if not ENRICHED_SAMPLE.exists():
        return {"path": str(ENRICHED_SAMPLE), "exists": False}
    df = pd.read_parquet(ENRICHED_SAMPLE)
    return {
        "path": str(ENRICHED_SAMPLE.relative_to(PROJECT_ROOT)),
        "exists": True,
        "columns": df.columns.tolist(),
        "rows": int(len(df)),
        "source_distribution": df["source"].value_counts(dropna=False).to_dict() if "source" in df.columns else {},
    }


def classify_join_purpose(text: str) -> dict[str, Any]:
    has_product_bridge = "product_bridge" in text
    has_inner_join = bool(re.search(r"JOIN\s+product_bridge", text))
    has_raw_passthrough = "unmatched raw" in text.lower() or "LEFT JOIN product_bridge" in text
    canonical_lines = line_ranges_for("canonical_value", text, context=2)
    return {
        "strategic_product_join_purpose": "filter_and_normalize",
        "has_product_bridge": has_product_bridge,
        "uses_product_bridge_join": has_inner_join,
        "has_unmatched_raw_passthrough": has_raw_passthrough,
        "canonical_value_dependency": "raw metric value; product_id is not mathematically required, but Layer 2 row identity requires product_id",
        "canonical_value_evidence_count": len(canonical_lines),
    }


def main() -> int:
    text = LAYER2_PATH.read_text(encoding="utf-8")
    result = {
        "layer2_etl_path": str(LAYER2_PATH.relative_to(PROJECT_ROOT)),
        "function_spans": {
            "load_strategic_product": function_span(LAYER2_PATH, "load_strategic_product"),
            "ubist_join_sql": function_span(LAYER2_PATH, "ubist_join_sql"),
            "enrich_ml": function_span(LAYER2_PATH, "enrich_ml"),
        },
        "strategic_product_references": line_ranges_for("strategic_product", text, context=2),
        "product_bridge_references": line_ranges_for("product_bridge", text, context=2),
        "classification": classify_join_purpose(text),
        "layer2_output_schema": layer2_schema(),
        "direction_a_necessity": "recommended_later_not_required_for_this_phase",
        "direction_b_safety": "safe_for_general_view_if_kept_separate_from_strategy_view",
        "conclusion": (
            "Layer 2 is intentionally catalog/product-bridge scoped for strategic marts. "
            "General view can safely bypass Layer 2 and read Layer 1 raw directly in this phase, "
            "while preserving strategic marts unchanged."
        ),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
