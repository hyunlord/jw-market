#!/usr/bin/env python3
"""Render optional matplotlib plots for cutoff investigation."""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from common import SAMPLES_DIR, ensure_dirs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default=str(SAMPLES_DIR / "plots"))
    args = parser.parse_args()

    ensure_dirs()
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    try:
        import matplotlib.pyplot as plt
    except Exception as exc:
        print(f"matplotlib unavailable: {exc}")
        return 0

    hist = pd.read_csv(SAMPLES_DIR / "score_histogram.csv")
    plt.figure(figsize=(10, 4))
    plt.bar(hist["score"], hist["count"], width=0.9)
    plt.title("Score Histogram")
    plt.xlabel("score")
    plt.ylabel("count")
    plt.tight_layout()
    plt.savefig(output / "score_histogram.png", dpi=150)
    plt.close()

    matrix = pd.read_csv(SAMPLES_DIR / "brand_cutoff_5y.csv")
    jw = matrix[matrix["brand_group"] == "jw"].head(30)
    cols = [c for c in jw.columns if c.startswith("ge_")]
    plt.figure(figsize=(9, max(4, len(jw) * 0.25)))
    plt.imshow(jw[cols], aspect="auto")
    plt.yticks(range(len(jw)), jw["brand"], fontsize=7)
    plt.xticks(range(len(cols)), cols, rotation=45)
    plt.colorbar(label="events")
    plt.title("JW Brand Cutoff Heatmap (5y)")
    plt.tight_layout()
    plt.savefig(output / "brand_cutoff_heatmap.png", dpi=150)
    plt.close()

    overlap = pd.read_csv(SAMPLES_DIR / "marker_overlap.csv")
    month = overlap[(overlap["granularity"] == "UBIST_month") & (overlap["cutoff"].isin([55, 60, 65, 70]))]
    pivot = month.pivot_table(index="time", columns="cutoff", values="jw_brands_with_marker", aggfunc="max")
    pivot.plot(figsize=(10, 4), marker="o")
    plt.title("JW Marker Overlap by Month")
    plt.xlabel("month")
    plt.ylabel("JW brands with marker")
    plt.tight_layout()
    plt.savefig(output / "marker_density.png", dpi=150)
    plt.close()
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
