"""Bounded periodic maintenance for persistent dynamic-market responses."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json

from pipeline.scripts.api.dynamic_market.runtime_cache import dynamic_response_cache_store


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-limit", type=int, default=100)
    parser.add_argument("--max-batches", type=int, default=10)
    parser.add_argument("--grace-seconds", type=int, default=300)
    args = parser.parse_args()

    results: list[dict[str, int]] = []
    for _ in range(args.max_batches):
        result = dynamic_response_cache_store.prune(
            grace_seconds=args.grace_seconds,
            batch_limit=args.batch_limit,
        )
        results.append(asdict(result))
        if result.selected == 0 or result.deleted == 0:
            break
    print(json.dumps(results, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
