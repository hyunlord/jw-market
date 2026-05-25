#!/usr/bin/env python3
"""Phase 27 validation for Ox/Gx catalog loading and strategic rank/MS.

This is the hard gate for the Phase 27 big-bang rebuild:

* UBIST processed parquet must retain the source ``Generic`` classification.
* ml_006/ml_007/ml_008 must expose source-derived Ox/Gx catalog values.
* ml_011 must keep its existing Ox/Biosimilar/Gx catalog values unchanged.
* Strategic ML/CD marts must store rank/MS at their own market scope.
* D.3 must surface Ox/Gx options for markets where the catalog enables it.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[3]
UBIST_LATEST_PARQUET = PROJECT_ROOT / "output" / "ubist" / "year=2026" / "month=04" / "data.parquet"
CATALOG_DIR = PROJECT_ROOT / "output" / "catalog"
BASE_URL = "http://127.0.0.1:8013"

GENERIC_TO_OX_GX = {
    "Original": "Ox",
    "개량신약(Super Generic)": "Ox",
    "Generic": "Gx",
    "First Generic": "Gx",
}
EXPECTED_GENERIC_VALUES = set(GENERIC_TO_OX_GX)
SOURCE_DERIVED_OX_GX_MARKETS = ("ml_006", "ml_007", "ml_008")
OX_GX_API_BRANDS = {
    "ml_006": "리바로",
    "ml_007": "리바로페노",
    "ml_008": "리바로하이",
    "ml_011": "악템라",
}
OX_GX_API_SOURCES = {
    "ml_011": "IQVIA",
}


@dataclass
class ValidationIssue:
    kind: str
    detail: dict[str, Any] = field(default_factory=dict)


def _present(value: Any) -> bool:
    if value is None:
        return False
    try:
        if pd.isna(value):
            return False
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    return bool(text) and text.lower() not in {"nan", "none", "null", "<na>"}


def _bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "t", "yes", "y"}
    return bool(value)


def validate_ubist_parquet_generic(path: Path = UBIST_LATEST_PARQUET) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if not path.exists():
        return [ValidationIssue("ubist_parquet_missing", {"path": str(path)})]

    frame = pd.read_parquet(path, columns=["Generic"])
    if "Generic" not in frame.columns:
        return [ValidationIssue("ubist_generic_column_missing", {"path": str(path)})]

    total = int(len(frame))
    non_null = int(frame["Generic"].map(_present).sum())
    null_rate = 1.0 - (non_null / total if total else 0.0)
    values = set(frame["Generic"].dropna().astype(str).str.strip().unique())

    if total == 0:
        issues.append(ValidationIssue("ubist_parquet_empty", {"path": str(path)}))
    if null_rate > 0.01:
        issues.append(
            ValidationIssue(
                "ubist_generic_null_rate_too_high",
                {"path": str(path), "rows": total, "non_null": non_null, "null_rate": null_rate},
            )
        )
    missing = sorted(EXPECTED_GENERIC_VALUES - values)
    if missing:
        issues.append(
            ValidationIssue(
                "ubist_generic_expected_values_missing",
                {"path": str(path), "values": sorted(values), "missing": missing},
            )
        )
    return issues


def validate_catalog_ox_gx(catalog_dir: Path = CATALOG_DIR) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    ml_market = pd.read_parquet(catalog_dir / "ml_market" / "ml_market.parquet")
    strategic_brand = pd.read_parquet(catalog_dir / "strategic_brand" / "strategic_brand.parquet")

    market_by_id = ml_market.set_index("ml_id", drop=False)
    for ml_id in SOURCE_DERIVED_OX_GX_MARKETS:
        if ml_id not in market_by_id.index:
            issues.append(ValidationIssue("ml_market_missing", {"ml_id": ml_id}))
            continue
        if not _bool(market_by_id.loc[ml_id].get("analyze_ox_gx")):
            issues.append(ValidationIssue("analyze_ox_gx_not_enabled", {"ml_id": ml_id}))

        rows = strategic_brand.loc[strategic_brand["ml_id"].astype(str) == ml_id].copy()
        values = rows["ox_gx"].dropna().astype(str).str.strip()
        bad = sorted(set(values) - {"Ox", "Gx"})
        null_count = int((~rows["ox_gx"].map(_present)).sum())
        if null_count:
            issues.append(ValidationIssue("catalog_ox_gx_null", {"ml_id": ml_id, "null_count": null_count, "rows": int(len(rows))}))
        if bad:
            issues.append(ValidationIssue("catalog_ox_gx_unexpected_values", {"ml_id": ml_id, "values": bad}))
        if not {"Ox", "Gx"}.issubset(set(values)):
            issues.append(
                ValidationIssue(
                    "catalog_ox_gx_missing_expected_classes",
                    {"ml_id": ml_id, "values": sorted(set(values))},
                )
            )

    ml006_jw = strategic_brand.loc[
        (strategic_brand["ml_id"].astype(str) == "ml_006")
        & strategic_brand.get("is_jw", False).map(_bool)
    ]
    for _, row in ml006_jw.iterrows():
        if str(row.get("ox_gx")) != "Ox":
            issues.append(
                ValidationIssue(
                    "ml006_jw_not_ox",
                    {"name": row.get("name"), "brand_id": row.get("brand_id"), "ox_gx": row.get("ox_gx")},
                )
            )

    ml011 = strategic_brand.loc[strategic_brand["ml_id"].astype(str) == "ml_011"]
    counts = ml011["ox_gx"].value_counts(dropna=False).to_dict()
    expected_counts = {"Ox": 14, "Biosimilar": 9, "Gx": 3}
    for label, expected in expected_counts.items():
        actual = int(counts.get(label, 0))
        if actual != expected:
            issues.append(
                ValidationIssue(
                    "ml011_ox_gx_changed",
                    {"label": label, "expected": expected, "actual": actual, "counts": {str(k): int(v) for k, v in counts.items()}},
                )
            )
    return issues


def validate_phase26_strategic_mart() -> list[ValidationIssue]:
    result = subprocess.run(
        ["python3", "pipeline/scripts/validation/phase26_mart_loading_pipeline.py"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=900,
    )
    if result.returncode == 0:
        return []
    return [
        ValidationIssue(
            "phase26_mart_rank_ms_issues",
            {
                "returncode": result.returncode,
                "stdout_tail": result.stdout[-4000:],
                "stderr_tail": result.stderr[-2000:],
            },
        )
    ]


def get_json(path: str, *, base_url: str) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(f"{base_url}{path}", timeout=45) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code in {400, 404, 422}:
            return None
        raise


def validate_api_d3_ox_gx_options(base_url: str = BASE_URL) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for ml_id, brand in OX_GX_API_BRANDS.items():
        encoded = urllib.parse.quote(brand)
        source = OX_GX_API_SOURCES.get(ml_id, "UBIST")
        payload = get_json(f"/api/cause/{encoded}?view=market_landscape&source={source}&measure=sales", base_url=base_url)
        if not payload or not payload.get("data"):
            issues.append(ValidationIssue("api_cause_missing", {"ml_id": ml_id, "brand": brand, "source": source}))
            continue
        by_level = payload["data"].get("level_top5_trend", {}).get("by_level") or {}
        level_payload = by_level.get("Ox/Gx")
        if not level_payload:
            issues.append(ValidationIssue("api_d3_ox_gx_level_missing", {"ml_id": ml_id, "brand": brand, "levels": sorted(by_level)}))
            continue
        options = set(level_payload.get("all_options") or [])
        expected = {"Ox", "Biosimilar", "Gx"} if ml_id == "ml_011" else {"Ox", "Gx"}
        if options != expected:
            issues.append(
                ValidationIssue(
                    "api_d3_ox_gx_options_wrong",
                    {"ml_id": ml_id, "brand": brand, "expected": sorted(expected), "actual": sorted(options)},
                )
            )
    return issues


def run(*, base_url: str = BASE_URL, skip_api: bool = False) -> dict[str, Any]:
    checks = {
        "ubist_generic": validate_ubist_parquet_generic(),
        "catalog_ox_gx": validate_catalog_ox_gx(),
        "phase26_mart": validate_phase26_strategic_mart(),
    }
    if not skip_api:
        checks["api_d3_ox_gx"] = validate_api_d3_ox_gx_options(base_url)

    issues = [asdict(issue) | {"check": check_name} for check_name, check_issues in checks.items() for issue in check_issues]
    return {
        "phase": "27",
        "validator": "oxgx_catalog_strategic_rank_ms",
        "checks": {name: len(check_issues) for name, check_issues in checks.items()},
        "issues": issues,
        "issues_count": len(issues),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--skip-api", action="store_true")
    parser.add_argument("--json-out")
    args = parser.parse_args()

    result = run(base_url=args.base_url, skip_api=args.skip_api)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)

    print("=== Phase 27 Ox/Gx + Strategic Mart Validation ===")
    print("checks=" + json.dumps(result["checks"], ensure_ascii=False, sort_keys=True))
    print(f"issues={result['issues_count']}")
    for issue in result["issues"][:50]:
        print(json.dumps(issue, ensure_ascii=False, sort_keys=True))
    return 1 if result["issues"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
