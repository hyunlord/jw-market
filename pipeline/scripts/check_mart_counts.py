#!/usr/bin/env python3
"""Print row counts for the six JSON Layer 3 marts."""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "etl"))

from layer3_compute_general_v3 import mariadb_connect


MARTS = [
    "mart_general_brand_metric",
    "mart_general_market_metric",
    "mart_strategic_ml_brand_metric",
    "mart_strategic_ml_market_metric",
    "mart_strategic_cd_brand_metric",
    "mart_strategic_cd_market_metric",
]


def main() -> int:
    conn = mariadb_connect()
    try:
        with conn.cursor() as cur:
            for table in MARTS:
                cur.execute(f"SELECT COUNT(*) AS row_count FROM {table}")
                print(f"{table}: {cur.fetchone()['row_count']:,}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
