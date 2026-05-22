#!/usr/bin/env python3
"""Build spec-aligned cache_brands from the Phase 1 strategic catalog."""

from __future__ import annotations

from cache_build_common import (
    CANONICAL_25,
    dump_payload,
    load_catalog,
    ml_to_strategy,
    parser,
    payload_size,
    replace_rows,
    source_list,
)


def main() -> None:
    args = parser(__doc__).parse_args()
    strategic_brand = load_catalog("strategic_brand")
    ml_market = load_catalog("ml_market").set_index("ml_id", drop=False)

    jw = strategic_brand[strategic_brand["is_jw"].astype(bool)].copy()
    actual = set(jw["canonical_name"].fillna(jw["name"]).astype(str))
    missing = CANONICAL_25 - actual
    extra = actual - CANONICAL_25
    if missing or extra:
        raise SystemExit(f"canonical brand mismatch: missing={sorted(missing)}, extra={sorted(extra)}")

    cards = []
    for _, row in jw.sort_values(["ml_id", "is_target", "brand_id"], ascending=[True, False, True]).iterrows():
        ml_id = str(row["ml_id"])
        market = ml_market.loc[ml_id].to_dict() if ml_id in ml_market.index else {}
        brand = str(row.get("canonical_name") or row.get("name"))
        sources = source_list(market.get("data_source"))
        cards.append(
            {
                "brand": brand,
                "brand_key": str(row.get("brand_key") or brand),
                "market_id": ml_to_strategy(ml_id),
                "ml_id": ml_id,
                "market_name": market.get("name"),
                "market_description": market.get("description"),
                "mkt_team": market.get("mkt_team"),
                "is_jw": True,
                "is_target": bool(row.get("is_target")),
                "sources": sources,
                "is_dual_source": len(sources) == 2,
                "rank": int(row.get("canonical_rank") or 0),
            }
        )

    payload = cards
    row = {
        "query_key": "default",
        "response_json": dump_payload(payload),
        "payload_size": payload_size(payload),
    }
    replace_rows("cache_brands", ["query_key", "response_json", "payload_size"], [row])
    if args.verbose:
        print(f"cache_brands default brand_count={len(cards)} payload_size={row['payload_size']}")


if __name__ == "__main__":
    main()
