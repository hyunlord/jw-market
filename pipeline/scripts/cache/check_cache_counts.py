#!/usr/bin/env python3
"""Print row counts and storage size for the four split Layer 4 cache tables."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from pipeline.scripts.api.db import connect


CACHE_TABLES = [
    "cache_brands",
    "cache_market_status",
    "cache_cause",
    "cache_deep_analysis",
]


def main() -> int:
    rows: list[tuple[str, int, float]] = []

    with connect() as conn:
        with conn.cursor() as cur:
            for table in CACHE_TABLES:
                cur.execute(f"SELECT COUNT(*) AS cnt FROM {table}")
                row_count = int(cur.fetchone()["cnt"])
                cur.execute(
                    """
                    SELECT ROUND((data_length + index_length) / 1024 / 1024, 2) AS size_mb
                    FROM information_schema.tables
                    WHERE table_schema = DATABASE()
                      AND table_name = %s
                    """,
                    (table,),
                )
                size_mb = float(cur.fetchone()["size_mb"] or 0)
                rows.append((table, row_count, size_mb))

    total_rows = sum(row_count for _, row_count, _ in rows)
    total_size_mb = sum(size_mb for _, _, size_mb in rows)

    print("| Table | Rows | Size MB |")
    print("|---|---:|---:|")
    for table, row_count, size_mb in rows:
        print(f"| {table} | {row_count:,} | {size_mb:,.2f} |")
    print(f"| **Total** | **{total_rows:,}** | **{total_size_mb:,.2f}** |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
