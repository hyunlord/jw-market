#!/usr/bin/env python3
"""Validate split Layer 4 cache sample rows against required schemas."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from pipeline.scripts.api.db import connect
from pipeline.scripts.api_response_builder.schemas import validate_response
from pipeline.scripts.api_response_builder.utils import json_dumps, parse_json


ENDPOINT_TABLES = {
    "brands": ("cache_brands", "CONCAT(view_type, '|', source)"),
    "market-status": ("cache_market_status", "CONCAT(view_type, '|', market_id, '|', source, '|', measure)"),
    "cause": ("cache_cause", "CONCAT(view_type, '|', brand_key, '|', market_id, '|', source, '|', measure)"),
    "deep-analysis": (
        "cache_deep_analysis",
        "CONCAT(view_type, '|', brand_key, '|', market_id, '|', source, '|', measure)",
    ),
}


def validate_endpoint(endpoint: str, sample: int) -> dict[str, object]:
    table, label_expr = ENDPOINT_TABLES[endpoint]
    failures = []
    checked = 0
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {label_expr} AS cache_label, response_json
                FROM {table}
                ORDER BY cache_label
                LIMIT %s
                """,
                (sample,),
            )
            for row in cur.fetchall():
                checked += 1
                response = parse_json(row["response_json"])
                missing = validate_response(endpoint, response)
                if missing:
                    failures.append({"cache_label": row["cache_label"], "missing": missing})
    return {"endpoint": endpoint, "checked": checked, "failures": failures}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-per-endpoint", type=int, default=25)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    results = [validate_endpoint(endpoint, args.sample_per_endpoint) for endpoint in ENDPOINT_TABLES]
    print(json_dumps({"results": results}))
    return 1 if any(item["failures"] for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
