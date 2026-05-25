#!/usr/bin/env python3
"""Build spec-aligned cache_brands from the Phase 1 strategic catalog."""

from __future__ import annotations

import sys

from cache_build_common import (
    CANONICAL_25,
    PROJECT_ROOT,
    dump_payload,
    load_catalog,
    parser,
    payload_size,
    replace_rows,
)

sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.scripts.api.metadata import BRAND_METADATA


def main() -> None:
    args = parser(__doc__).parse_args()
    strategic_brand = load_catalog("strategic_brand")

    jw = strategic_brand[strategic_brand["is_jw"].astype(bool)].copy()
    actual = set(jw["canonical_name"].fillna(jw["name"]).astype(str))
    missing = CANONICAL_25 - actual
    extra = actual - CANONICAL_25
    if missing or extra:
        raise SystemExit(f"canonical brand mismatch: missing={sorted(missing)}, extra={sorted(extra)}")

    metadata_brands = {meta.brand for meta in BRAND_METADATA}
    if metadata_brands != CANONICAL_25:
        raise SystemExit(
            f"brand metadata mismatch: missing={sorted(CANONICAL_25 - metadata_brands)}, "
            f"extra={sorted(metadata_brands - CANONICAL_25)}"
        )

    payload = [meta.to_response() for meta in BRAND_METADATA]
    row = {
        "query_key": "default",
        "response_json": dump_payload(payload),
        "payload_size": payload_size(payload),
    }
    replace_rows("cache_brands", ["query_key", "response_json", "payload_size"], [row])
    if args.verbose:
        print(f"cache_brands default brand_count={len(payload)} payload_size={row['payload_size']}")


if __name__ == "__main__":
    main()
