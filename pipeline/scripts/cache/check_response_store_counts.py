#!/usr/bin/env python3
"""Print response_store row counts and key metadata."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from pipeline.scripts.api.db import connect
from pipeline.scripts.api_response_builder.utils import json_dumps


def main() -> int:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT endpoint, COUNT(*) AS row_count,
                       COUNT(DISTINCT view_type) AS views,
                       COUNT(DISTINCT source) AS sources,
                       COUNT(DISTINCT measure) AS measures,
                       SUM(size_bytes) AS total_bytes
                FROM response_store
                GROUP BY endpoint
                ORDER BY endpoint
                """
            )
            by_endpoint = list(cur.fetchall())
            cur.execute("SELECT COUNT(*) AS total_rows, SUM(size_bytes) AS total_bytes FROM response_store")
            total = cur.fetchone()
            cur.execute(
                """
                SELECT endpoint, view_type, source, measure, COUNT(*) AS row_count
                FROM response_store
                GROUP BY endpoint, view_type, source, measure
                ORDER BY endpoint, view_type, source, measure
                LIMIT 100
                """
            )
            breakdown = list(cur.fetchall())
    print(json_dumps({"total": total, "by_endpoint": by_endpoint, "breakdown_sample": breakdown}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
