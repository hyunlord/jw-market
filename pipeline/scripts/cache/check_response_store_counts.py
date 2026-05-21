#!/usr/bin/env python3
"""Print split Layer 4 cache row counts and key metadata."""

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
                SELECT 'brands' AS endpoint, COUNT(*) AS row_count,
                       COUNT(DISTINCT view_type) AS views,
                       COUNT(DISTINCT source) AS sources,
                       0 AS measures,
                       SUM(payload_size) AS total_bytes
                FROM cache_brands
                UNION ALL
                SELECT 'market-status' AS endpoint, COUNT(*) AS row_count,
                       COUNT(DISTINCT view_type) AS views,
                       COUNT(DISTINCT source) AS sources,
                       COUNT(DISTINCT measure) AS measures,
                       SUM(payload_size) AS total_bytes
                FROM cache_market_status
                UNION ALL
                SELECT 'cause' AS endpoint, COUNT(*) AS row_count,
                       COUNT(DISTINCT view_type) AS views,
                       COUNT(DISTINCT source) AS sources,
                       COUNT(DISTINCT measure) AS measures,
                       SUM(payload_size) AS total_bytes
                FROM cache_cause
                UNION ALL
                SELECT 'deep-analysis' AS endpoint, COUNT(*) AS row_count,
                       COUNT(DISTINCT view_type) AS views,
                       COUNT(DISTINCT source) AS sources,
                       COUNT(DISTINCT measure) AS measures,
                       SUM(payload_size) AS total_bytes
                FROM cache_deep_analysis
                """
            )
            by_endpoint = list(cur.fetchall())
            cur.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM cache_brands)
                  + (SELECT COUNT(*) FROM cache_market_status)
                  + (SELECT COUNT(*) FROM cache_cause)
                  + (SELECT COUNT(*) FROM cache_deep_analysis) AS total_rows,
                  (SELECT COALESCE(SUM(payload_size), 0) FROM cache_brands)
                  + (SELECT COALESCE(SUM(payload_size), 0) FROM cache_market_status)
                  + (SELECT COALESCE(SUM(payload_size), 0) FROM cache_cause)
                  + (SELECT COALESCE(SUM(payload_size), 0) FROM cache_deep_analysis) AS total_bytes
                """
            )
            total = cur.fetchone()
            cur.execute(
                """
                SELECT 'brands' AS endpoint, view_type, source, NULL AS measure, COUNT(*) AS row_count
                FROM cache_brands
                GROUP BY view_type, source
                UNION ALL
                SELECT 'market-status' AS endpoint, view_type, source, measure, COUNT(*) AS row_count
                FROM cache_market_status
                GROUP BY view_type, source, measure
                UNION ALL
                SELECT 'cause' AS endpoint, view_type, source, measure, COUNT(*) AS row_count
                FROM cache_cause
                GROUP BY view_type, source, measure
                UNION ALL
                SELECT 'deep-analysis' AS endpoint, view_type, source, measure, COUNT(*) AS row_count
                FROM cache_deep_analysis
                GROUP BY view_type, source, measure
                ORDER BY endpoint, view_type, source, measure
                """
            )
            breakdown = list(cur.fetchall())
    print(json_dumps({"total": total, "by_endpoint": by_endpoint, "breakdown_sample": breakdown}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
