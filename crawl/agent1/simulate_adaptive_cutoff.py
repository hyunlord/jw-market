#!/usr/bin/env python3
"""Simulate adaptive cutoff outcomes for JW canonical brands."""

from __future__ import annotations

import argparse
import csv
from datetime import date, timedelta
from pathlib import Path
from statistics import mean
from typing import Any

import pandas as pd
import pymysql

from adaptive_cutoff import adaptive_cutoff


def jw_brands(catalog_path: Path) -> list[dict[str, str | None]]:
    catalog = pd.read_parquet(catalog_path)
    jw = catalog[catalog["is_jw"] == True].copy()  # noqa: E712
    jw = jw.drop_duplicates(subset=["name"]).sort_values("name")
    return [
        {"brand": str(row["name"]), "ml_id": None if pd.isna(row.get("ml_id")) else str(row.get("ml_id"))}
        for _, row in jw.iterrows()
    ]


def score_stats(events: list[dict[str, Any]]) -> tuple[int | None, int | None, float | None]:
    if not events:
        return None, None, None
    scores = [int(event["score"]) for event in events]
    return min(scores), max(scores), round(mean(scores), 1)


def query_events(cursor: Any, brand: str) -> list[dict[str, Any]]:
    cursor.execute(
        """
        SELECT e.event_id, e.date, ebs.score, ebs.ml_id
        FROM event_brand_scores ebs
        JOIN events e ON ebs.event_id = e.event_id
        WHERE ebs.brand_canonical = %s
        ORDER BY ebs.score DESC, e.date DESC, e.event_id
        """,
        (brand,),
    )
    return list(cursor.fetchall())


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-host", default="127.0.0.1")
    parser.add_argument("--db-port", type=int, default=3308)
    parser.add_argument("--db-name", default="jw_mart")
    parser.add_argument("--db-user", default="root")
    parser.add_argument("--db-password", default="")
    parser.add_argument("--catalog", type=Path, default=Path("output/catalog/strategic_brand/strategic_brand.parquet"))
    parser.add_argument("--output-panel", type=Path, required=True)
    parser.add_argument("--output-marker", type=Path, required=True)
    args = parser.parse_args()

    brands = jw_brands(args.catalog)
    recent_cutoff = date.today() - timedelta(days=365)
    panel_rows: list[dict[str, Any]] = []
    marker_rows: list[dict[str, Any]] = []

    conn = pymysql.connect(
        host=args.db_host,
        port=args.db_port,
        user=args.db_user,
        password=args.db_password,
        database=args.db_name,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )
    try:
        with conn.cursor() as cursor:
            for brand_info in brands:
                brand = str(brand_info["brand"])
                events_5y = query_events(cursor, brand)
                panel_events, panel_cutoff = adaptive_cutoff(
                    events_5y,
                    target_min=10,
                    target_max=50,
                    init_cutoff=35,
                )
                panel_min, panel_max, panel_avg = score_stats(panel_events)
                panel_rows.append(
                    {
                        "brand": brand,
                        "ml_id": brand_info["ml_id"] or "",
                        "total_events_5y": len(events_5y),
                        "cutoff_initial": 35,
                        "cutoff_applied": panel_cutoff,
                        "events_after": len(panel_events),
                        "events_score_min": panel_min if panel_min is not None else "",
                        "events_score_max": panel_max if panel_max is not None else "",
                        "events_score_avg": panel_avg if panel_avg is not None else "",
                    }
                )

                events_1y = [
                    event
                    for event in events_5y
                    if event.get("date") is not None and event["date"] >= recent_cutoff
                ]
                markers, marker_cutoff = adaptive_cutoff(
                    events_1y,
                    target_min=3,
                    target_max=15,
                    init_cutoff=75,
                )
                marker_min, marker_max, _ = score_stats(markers)
                marker_rows.append(
                    {
                        "brand": brand,
                        "ml_id": brand_info["ml_id"] or "",
                        "total_events_1y": len(events_1y),
                        "cutoff_initial": 75,
                        "cutoff_applied": marker_cutoff,
                        "markers_after": len(markers),
                        "markers_score_min": marker_min if marker_min is not None else "",
                        "markers_score_max": marker_max if marker_max is not None else "",
                    }
                )
    finally:
        conn.close()

    write_csv(
        args.output_panel,
        [
            "brand",
            "ml_id",
            "total_events_5y",
            "cutoff_initial",
            "cutoff_applied",
            "events_after",
            "events_score_min",
            "events_score_max",
            "events_score_avg",
        ],
        panel_rows,
    )
    write_csv(
        args.output_marker,
        [
            "brand",
            "ml_id",
            "total_events_1y",
            "cutoff_initial",
            "cutoff_applied",
            "markers_after",
            "markers_score_min",
            "markers_score_max",
        ],
        marker_rows,
    )
    print(f"wrote {args.output_panel} ({len(panel_rows)} rows)")
    print(f"wrote {args.output_marker} ({len(marker_rows)} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

