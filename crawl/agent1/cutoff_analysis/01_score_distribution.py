#!/usr/bin/env python3
"""Build score histogram and score distribution summaries."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from common import SAMPLES_DIR, describe_counts, ensure_dirs, load_matches, score_to_tier, write_df


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-csv", default=str(SAMPLES_DIR / "score_histogram.csv"))
    parser.add_argument("--summary-json", default=str(SAMPLES_DIR / "score_distribution_summary.json"))
    args = parser.parse_args()

    ensure_dirs()
    df = load_matches()
    total = len(df)
    hist = df["score"].value_counts().rename_axis("score").reset_index(name="count").sort_values("score")
    all_scores = pd.DataFrame({"score": range(int(df["score"].min()), int(df["score"].max()) + 1)})
    hist = all_scores.merge(hist, on="score", how="left").fillna({"count": 0})
    hist["count"] = hist["count"].astype(int)
    hist["cum_count"] = hist["count"].cumsum()
    hist["pct"] = (hist["count"] / max(total, 1) * 100).round(4)
    hist["cum_pct"] = (hist["cum_count"] / max(total, 1) * 100).round(4)
    hist["tier"] = hist["score"].map(score_to_tier)
    write_df(hist, Path(args.output_csv))

    by_group = {}
    for group, group_df in df.groupby("brand_group"):
        by_group[group] = {
            "matches": int(len(group_df)),
            "unique_brands": int(group_df["brand"].nunique()),
            "mean": round(float(group_df["score"].mean()), 2),
            "median": round(float(group_df["score"].median()), 2),
            "std": round(float(group_df["score"].std(ddof=0)), 2),
            "quartiles": {
                "p25": round(float(group_df["score"].quantile(0.25)), 2),
                "p75": round(float(group_df["score"].quantile(0.75)), 2),
            },
        }
    by_year = {
        str(year): {
            "matches": int(len(year_df)),
            "mean": round(float(year_df["score"].mean()), 2),
            "median": round(float(year_df["score"].median()), 2),
            "tier_distribution": year_df["score_tier"].value_counts().sort_index().to_dict(),
        }
        for year, year_df in df.groupby("year", dropna=True)
    }
    summary = {
        "total_matches": total,
        "unique_brands": int(df["brand"].nunique()),
        "mean": round(float(df["score"].mean()), 2),
        "median": round(float(df["score"].median()), 2),
        "std": round(float(df["score"].std(ddof=0)), 2),
        "min": int(df["score"].min()),
        "max": int(df["score"].max()),
        "quartiles": {
            "p25": round(float(df["score"].quantile(0.25)), 2),
            "p75": round(float(df["score"].quantile(0.75)), 2),
        },
        "tier_distribution": df["score_tier"].value_counts().sort_index().to_dict(),
        "by_group": by_group,
        "by_year": by_year,
        "brand_event_count_distribution": {
            group: describe_counts(group_df.groupby("brand").size())
            for group, group_df in df.groupby("brand_group")
        },
    }
    Path(args.summary_json).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ["total_matches", "unique_brands", "mean", "median", "std"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
