#!/usr/bin/env python3
"""Validate response_store sample rows against required top-level schemas."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from pipeline.scripts.api.db import connect
from pipeline.scripts.api_response_builder.schemas import validate_response
from pipeline.scripts.api_response_builder.utils import json_dumps, parse_json


ENDPOINTS = ("brands", "market-status", "cause", "deep-analysis")


def validate_endpoint(endpoint: str, sample: int) -> dict[str, object]:
    failures = []
    checked = 0
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT cache_key, response_json
                FROM response_store
                WHERE endpoint = %s
                ORDER BY cache_key
                LIMIT %s
                """,
                (endpoint, sample),
            )
            for row in cur.fetchall():
                checked += 1
                response = parse_json(row["response_json"])
                missing = validate_response(endpoint, response)
                if missing:
                    failures.append({"cache_key": row["cache_key"], "missing": missing})
    return {"endpoint": endpoint, "checked": checked, "failures": failures}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-per-endpoint", type=int, default=25)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    results = [validate_endpoint(endpoint, args.sample_per_endpoint) for endpoint in ENDPOINTS]
    print(json_dumps({"results": results}))
    return 1 if any(item["failures"] for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
