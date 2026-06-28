#!/usr/bin/env python3
"""Build brand-by-cutoff matrices for 5y and recent-1y windows."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from common import RECENT_CUTOFF, SAMPLES_DIR, describe_counts, ensure_dirs, load_matches, write_df


CUTOFF_5Y = [10, 15, 20, 25, 30, 35, 40, 45, 50]
CUTOFF_1Y = [40, 45, 50, 55, 60, 65, 70, 75, 80]


def build_matrix(df: pd.DataFrame, cutoffs: list[int], total_col: str) -> pd.DataFrame:
    brands = (
        df.groupby(["brand", "brand_group"], dropna=False)
        .size()
        .reset_index(name=total_col)
        .sort_values(["brand_group", total_col, "brand"], ascending=[True, False, True])
    )
    for cutoff in cutoffs:
        counts = df[df["score"] >= cutoff].groupby("brand").size().rename(f"ge_{cutoff}")
        brands = brands.merge(counts, on="brand", how="left")
        brands[f"ge_{cutoff}"] = brands[f"ge_{cutoff}"].fillna(0).astype(int)
    return brands


def summarize(matrix: pd.DataFrame, total_col: str, cutoffs: list[int]) -> dict[str, object]:
    result = {}
    for group, group_df in matrix.groupby("brand_group"):
        group_summary = {"brand_count": int(len(group_df)), total_col: describe_counts(group_df[total_col])}
        for cutoff in cutoffs:
            col = f"ge_{cutoff}"
            group_summary[col] = describe_counts(group_df[col])
            group_summary[f"{col}_zero_brands"] = int((group_df[col] == 0).sum())
        result[group] = group_summary
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-5y", default=str(SAMPLES_DIR / "brand_cutoff_5y.csv"))
    parser.add_argument("--output-1y", default=str(SAMPLES_DIR / "brand_cutoff_1y.csv"))
    parser.add_argument("--summary-json", default=str(SAMPLES_DIR / "brand_cutoff_summary.json"))
    args = parser.parse_args()

    ensure_dirs()
    df = load_matches()
    recent = df[(df["date"].notna()) & (df["date"] >= RECENT_CUTOFF)]
    matrix_5y = build_matrix(df, CUTOFF_5Y, "total_5y")
    matrix_1y = build_matrix(recent, CUTOFF_1Y, "total_1y")
    write_df(matrix_5y, Path(args.output_5y))
    write_df(matrix_1y, Path(args.output_1y))
    summary = {
        "cutoffs_5y": CUTOFF_5Y,
        "cutoffs_1y": CUTOFF_1Y,
        "matched_brand_count_5y": int(matrix_5y["brand"].nunique()),
        "matched_brand_count_1y": int(matrix_1y["brand"].nunique()),
        "summary_5y": summarize(matrix_5y, "total_5y", CUTOFF_5Y),
        "summary_1y": summarize(matrix_1y, "total_1y", CUTOFF_1Y),
    }
    Path(args.summary_json).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"brands_5y": summary["matched_brand_count_5y"], "brands_1y": summary["matched_brand_count_1y"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
