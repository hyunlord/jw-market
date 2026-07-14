#!/usr/bin/env python3
"""Build spec-aligned cache_brands from the Phase 1 strategic catalog."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from pipeline.scripts.etl.cache_build_common import (
    CANONICAL_25,
    catalog_input_manifest,
    current_build_sha,
    decode_json,
    dump_payload,
    load_catalog,
    parser,
    payload_size,
    replace_rows,
)

from pipeline.scripts.api.metadata import BRAND_METADATA, build_brand_metadata_payload
from pipeline.scripts.api.brand_source_options import brand_source_options
from pipeline.scripts.api.deep_analysis_context import public_source_labels


def _catalog_atc_codes_by_market(ml_market: object) -> dict[str, list[str]]:
    by_market: dict[str, list[str]] = {}
    for row in ml_market.to_dict("records"):
        ml_id = str(row.get("ml_id") or "").strip()
        market_id = f"strategy_{ml_id.removeprefix('ml_')}"
        raw_codes = decode_json(row.get("atc_codes_json"))
        if not ml_id or not isinstance(raw_codes, list):
            raise SystemExit(f"invalid ml market ATC catalog row: ml_id={ml_id!r}")
        codes = [str(code).strip() for code in raw_codes if str(code).strip()]
        if not codes:
            raise SystemExit(f"empty ml market ATC catalog row: ml_id={ml_id}")
        by_market[market_id] = codes
    return by_market


def _brand_payload(
    ml_market: object,
    source_lists_by_brand: dict[str, dict[str, list[str]]],
) -> list[dict[str, object]]:
    atc_codes_by_market = _catalog_atc_codes_by_market(ml_market)
    try:
        payload = build_brand_metadata_payload(atc_codes_by_market)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    expected = {str(item["brand"]) for item in payload}
    missing = sorted(expected - source_lists_by_brand.keys())
    unexpected = sorted(source_lists_by_brand.keys() - expected)
    if missing or unexpected:
        raise SystemExit(
            f"source evidence missing={missing}, unexpected={unexpected}"
        )
    enriched: list[dict[str, object]] = []
    for item in payload:
        brand = str(item["brand"])
        evidence = source_lists_by_brand[brand]
        general_sources = list(evidence.get("general_sources") or [])
        strategic_sources = list(evidence.get("strategic_sources") or [])
        sources = public_source_labels((*general_sources, *strategic_sources))
        row: dict[str, object] = {}
        for key, value in item.items():
            if key == "sources":
                row["sources"] = sources
                row["strategic_sources"] = strategic_sources
                row["general_sources"] = general_sources
            else:
                row[key] = value
        enriched.append(row)
    return enriched


def _source_lists_by_brand() -> dict[str, dict[str, list[str]]]:
    result: dict[str, dict[str, list[str]]] = {}
    for metadata in BRAND_METADATA:
        _, general_sources, strategic_sources = brand_source_options(metadata.brand)
        result[metadata.brand] = {
            "general_sources": general_sources,
            "strategic_sources": strategic_sources,
        }
    return result


def main() -> None:
    cli = parser(__doc__)
    cli.add_argument(
        "--target-table",
        default="cache_brands",
        help="Destination table. Use schema.table for test cache refreshes.",
    )
    args = cli.parse_args()
    strategic_brand = load_catalog("strategic_brand")
    ml_market = load_catalog("ml_market")

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

    payload = _brand_payload(ml_market, _source_lists_by_brand())
    row = {
        "query_key": "default",
        "response_json": dump_payload(payload),
        "payload_size": payload_size(payload),
        "build_sha": current_build_sha(),
        "input_manifest_json": catalog_input_manifest(
            {"strategic_brand": strategic_brand, "ml_market": ml_market}
        ),
    }
    replace_rows(
        args.target_table,
        ["query_key", "response_json", "payload_size", "build_sha", "input_manifest_json"],
        [row],
    )
    if args.verbose:
        print(f"cache_brands default brand_count={len(payload)} payload_size={row['payload_size']}")


if __name__ == "__main__":
    main()
