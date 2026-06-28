#!/usr/bin/env python3
"""Compute recent-1y UBIST monthly and IQVIA quarterly marker density."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from common import IQVIA_QUARTERS, RECENT_CUTOFF, SAMPLES_DIR, UBIST_MONTHS, ensure_dirs, load_matches, write_df


CUTOFFS = [40, 50, 55, 60, 65, 70, 75, 80]


def brand_groups(df: pd.DataFrame) -> pd.DataFrame:
    return df[["brand", "brand_group"]].drop_duplicates().sort_values(["brand_group", "brand"])


def density_rows(df: pd.DataFrame, periods: list[str], period_col: str, period_name: str) -> pd.DataFrame:
    brands = brand_groups(df)
    rows = []
    for cutoff in CUTOFFS:
        filtered = df[(df["score"] >= cutoff) & (df[period_col].isin(periods))]
        counts = filtered.groupby(["brand", period_col]).size().to_dict()
        for _, brand_row in brands.iterrows():
            for period in periods:
                rows.append(
                    {
                        "brand": brand_row["brand"],
                        "brand_group": brand_row["brand_group"],
                        "cutoff": cutoff,
                        period_name: period,
                        "marker_count": int(counts.get((brand_row["brand"], period), 0)),
                    }
                )
    return pd.DataFrame(rows)


def overlap(df: pd.DataFrame, monthly: pd.DataFrame, quarterly: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for cutoff in CUTOFFS:
        for month in UBIST_MONTHS:
            slice_df = monthly[(monthly["cutoff"] == cutoff) & (monthly["month"] == month)]
            rows.append(
                {
                    "granularity": "UBIST_month",
                    "time": month,
                    "cutoff": cutoff,
                    "total_markers": int(slice_df["marker_count"].sum()),
                    "n_brands_with_marker": int((slice_df["marker_count"] > 0).sum()),
                    "jw_total_markers": int(slice_df[slice_df["brand_group"] == "jw"]["marker_count"].sum()),
                    "jw_brands_with_marker": int(((slice_df["brand_group"] == "jw") & (slice_df["marker_count"] > 0)).sum()),
                }
            )
        for quarter in IQVIA_QUARTERS:
            slice_df = quarterly[(quarterly["cutoff"] == cutoff) & (quarterly["quarter"] == quarter)]
            rows.append(
                {
                    "granularity": "IQVIA_quarter",
                    "time": quarter,
                    "cutoff": cutoff,
                    "total_markers": int(slice_df["marker_count"].sum()),
                    "n_brands_with_marker": int((slice_df["marker_count"] > 0).sum()),
                    "jw_total_markers": int(slice_df[slice_df["brand_group"] == "jw"]["marker_count"].sum()),
                    "jw_brands_with_marker": int(((slice_df["brand_group"] == "jw") & (slice_df["marker_count"] > 0)).sum()),
                }
            )
    return pd.DataFrame(rows)


def scenario_stats(monthly: pd.DataFrame, quarterly: pd.DataFrame, overlap_df: pd.DataFrame) -> dict[str, dict[str, float | int]]:
    stats = {}
    jw_monthly = monthly[monthly["brand_group"] == "jw"]
    jw_quarterly = quarterly[quarterly["brand_group"] == "jw"]
    for cutoff in CUTOFFS:
        jm = jw_monthly[jw_monthly["cutoff"] == cutoff]
        jq = jw_quarterly[jw_quarterly["cutoff"] == cutoff]
        om = overlap_df[(overlap_df["granularity"] == "UBIST_month") & (overlap_df["cutoff"] == cutoff)]
        oq = overlap_df[(overlap_df["granularity"] == "IQVIA_quarter") & (overlap_df["cutoff"] == cutoff)]
        by_brand_month = jm.groupby("brand")["marker_count"].sum()
        stats[str(cutoff)] = {
            "jw_avg_marker_per_brand_month": round(float(jm["marker_count"].mean()), 3),
            "jw_avg_marker_per_brand_quarter": round(float(jq["marker_count"].mean()), 3),
            "jw_zero_marker_brands_1y": int((by_brand_month == 0).sum()),
            "jw_brand_count": int(by_brand_month.shape[0]),
            "max_overlap_month_brands_all": int(om["n_brands_with_marker"].max()),
            "max_overlap_month_brands_jw": int(om["jw_brands_with_marker"].max()),
            "max_overlap_quarter_brands_all": int(oq["n_brands_with_marker"].max()),
            "max_overlap_quarter_brands_jw": int(oq["jw_brands_with_marker"].max()),
            "max_total_markers_month_all": int(om["total_markers"].max()),
            "max_total_markers_quarter_all": int(oq["total_markers"].max()),
        }
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-monthly", default=str(SAMPLES_DIR / "marker_density_ubist_monthly.csv"))
    parser.add_argument("--output-quarterly", default=str(SAMPLES_DIR / "marker_density_iqvia_quarterly.csv"))
    parser.add_argument("--output-overlap", default=str(SAMPLES_DIR / "marker_overlap.csv"))
    parser.add_argument("--summary-json", default=str(SAMPLES_DIR / "marker_density_summary.json"))
    args = parser.parse_args()

    ensure_dirs()
    df = load_matches()
    recent = df[(df["date"].notna()) & (df["date"] >= RECENT_CUTOFF)]
    monthly = density_rows(recent, UBIST_MONTHS, "period_ubist", "month")
    quarterly = density_rows(recent, IQVIA_QUARTERS, "period_iqvia", "quarter")
    overlap_df = overlap(recent, monthly, quarterly)
    write_df(monthly, Path(args.output_monthly))
    write_df(quarterly, Path(args.output_quarterly))
    write_df(overlap_df, Path(args.output_overlap))
    stats = scenario_stats(monthly, quarterly, overlap_df)
    Path(args.summary_json).write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(stats, ensure_ascii=False)[:1000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
