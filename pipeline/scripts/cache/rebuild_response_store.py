#!/usr/bin/env python3
"""Rebuild Layer 4 response_store from the six JSON Layer 3 marts."""

from __future__ import annotations

import argparse
import time
import sys
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from pipeline.scripts.api.db import connect
from pipeline.scripts.api_response_builder import (
    build_brands_response,
    build_cause_response_from_rows,
    build_deep_analysis_response_from_rows,
    build_market_status_response_from_row,
)
from pipeline.scripts.api_response_builder.schemas import validate_response
from pipeline.scripts.api_response_builder.utils import (
    BRAND_MARTS,
    json_dumps,
    market_id_for_brand_row,
    market_key,
    now_iso,
)


BUILDER_VERSION = "phase16g4-fix-cache-v1"


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def summarize_response(endpoint: str, cache_key: str, response: dict[str, Any]) -> dict[str, Any]:
    if endpoint == "brands":
        return {
            "cache_key": cache_key,
            "total_count": response.get("total_count"),
            "sample_brand_keys": [item.get("brand_key") for item in (response.get("brands") or [])[:5]],
        }
    if endpoint == "market-status":
        market_size = response.get("market_size_series") or {}
        target = response.get("target_customer_competition") or {}
        return {
            "cache_key": cache_key,
            "market_id": response.get("market_id"),
            "period_count": len(market_size),
            "target_source_type": target.get("source_type"),
        }
    if endpoint == "cause":
        target = response.get("target_customer_competition") or {}
        return {
            "cache_key": cache_key,
            "brand_key": response.get("brand_key"),
            "market_id": response.get("market_id"),
            "latest_period": (response.get("kpi") or {}).get("latest_period"),
            "target_source_type": target.get("source_type"),
        }
    if endpoint == "deep-analysis":
        data = response.get("data") or {}
        return {
            "cache_key": cache_key,
            "brand_key": response.get("brand_key"),
            "market_id": response.get("market_id"),
            "data_keys": sorted(data.keys()),
        }
    return {"cache_key": cache_key}


def insert_response_rows(rows: list[dict[str, Any]], batch_size: int = 1000) -> None:
    if not rows:
        return
    sql = """
        INSERT INTO response_store (
          cache_key, endpoint, brand_name, brand_key, market_id, period_yyyymm,
          view, view_type, source, measure, response_json, payload, ttl_seconds,
          computed_at, expires_at, computation_ms, size_bytes
        )
        VALUES (
          %(cache_key)s, %(endpoint)s, %(brand_name)s, %(brand_key)s, %(market_id)s,
          %(period_yyyymm)s, %(view)s, %(view_type)s, %(source)s, %(measure)s,
          %(response_json)s, %(payload)s, %(ttl_seconds)s, NOW(), NULL,
          %(computation_ms)s, %(size_bytes)s
        )
        ON DUPLICATE KEY UPDATE
          endpoint = VALUES(endpoint),
          brand_name = VALUES(brand_name),
          brand_key = VALUES(brand_key),
          market_id = VALUES(market_id),
          period_yyyymm = VALUES(period_yyyymm),
          view = VALUES(view),
          view_type = VALUES(view_type),
          source = VALUES(source),
          measure = VALUES(measure),
          response_json = VALUES(response_json),
          payload = VALUES(payload),
          ttl_seconds = VALUES(ttl_seconds),
          computed_at = NOW(),
          expires_at = NULL,
          computation_ms = VALUES(computation_ms),
          size_bytes = VALUES(size_bytes)
    """
    with connect() as conn:
        with conn.cursor() as cur:
            for start in range(0, len(rows), batch_size):
                cur.executemany(sql, rows[start : start + batch_size])
        conn.commit()


def response_row(
    *,
    endpoint: str,
    cache_key: str,
    response: dict[str, Any],
    view_type: str | None = None,
    source: str | None = None,
    measure: str | None = None,
    brand_name: str | None = None,
    brand_key: str | None = None,
    market_id: str | None = None,
    payload: dict[str, Any] | None = None,
    computation_ms: int | None = None,
) -> dict[str, Any]:
    response_json = json_dumps(response)
    return {
        "cache_key": cache_key,
        "endpoint": endpoint,
        "brand_name": brand_name,
        "brand_key": brand_key,
        "market_id": market_id,
        "period_yyyymm": None,
        "view": view_type,
        "view_type": view_type,
        "source": source,
        "measure": measure,
        "response_json": response_json,
        "payload": json_dumps(payload or {}),
        "ttl_seconds": 86400,
        "computation_ms": computation_ms,
        "size_bytes": len(response_json.encode("utf-8")),
    }


def truncate_response_store() -> None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE response_store")
        conn.commit()


def rebuild_brands_cache(dry_run: bool = False) -> dict[str, Any]:
    specs = [
        (None, None),
        ("general", None),
        ("strategic_ml", None),
        ("strategic_cd", None),
        (None, "ubist"),
        (None, "iqvia_nsa"),
    ]
    rows: list[dict[str, Any]] = []
    samples: dict[str, Any] = {}
    for view_type, source in specs:
        start = time.perf_counter()
        response = build_brands_response(view_type=view_type, source=source)
        elapsed = int((time.perf_counter() - start) * 1000)
        key_view = view_type or "all"
        key_source = source or "all"
        cache_key = f"endpoint=brands|view={key_view}|source={key_source}"
        missing = validate_response("brands", response)
        if missing:
            raise RuntimeError(f"{cache_key}: missing keys {missing}")
        samples[cache_key] = summarize_response("brands", cache_key, response)
        rows.append(
            response_row(
                endpoint="brands",
                cache_key=cache_key,
                response=response,
                view_type=view_type or "all",
                source=source,
                payload={"builder_version": BUILDER_VERSION, "computed_at": now_iso()},
                computation_ms=elapsed,
            )
        )
    if not dry_run:
        insert_response_rows(rows)
    return {"rows": len(rows), "samples": samples}


def load_market_rows() -> dict[tuple[str, str | None, str | None, str | None], dict[str, Any]]:
    markets: dict[tuple[str, str | None, str | None, str | None], dict[str, Any]] = {}
    with connect() as conn:
        with conn.cursor() as cur:
            for view_type, cfg in BRAND_MARTS.items():
                mart = cfg["market_mart"]
                id_col = cfg["market_id_col"]
                cur.execute(f"SELECT * FROM {mart}")
                for row in cur.fetchall():
                    markets[market_key(view_type, row.get(id_col), row.get("source"), row.get("measure"))] = row
    return markets


def rebuild_market_status_cache(
    markets: dict[tuple[str, str | None, str | None, str | None], dict[str, Any]],
    *,
    dry_run: bool = False,
    limit: int | None = None,
    batch_size: int = 1000,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    samples: list[dict[str, Any]] = []
    count = 0
    for (view_type, market_id, source, measure), market_row in markets.items():
        if limit is not None and count >= limit:
            break
        start = time.perf_counter()
        response = build_market_status_response_from_row(view_type, market_row)
        elapsed = int((time.perf_counter() - start) * 1000)
        missing = validate_response("market-status", response)
        if missing:
            raise RuntimeError(f"market-status {view_type}/{market_id}/{source}/{measure}: missing {missing}")
        cache_key = f"endpoint=market-status|view={view_type}|market_id={market_id}|source={source}|measure={measure}"
        rows.append(
            response_row(
                endpoint="market-status",
                cache_key=cache_key,
                response=response,
                view_type=view_type,
                source=source,
                measure=measure,
                market_id=market_id,
                payload={
                    "builder_version": BUILDER_VERSION,
                    "source_mart": BRAND_MARTS[view_type]["market_mart"],
                    "computed_at": now_iso(),
                },
                computation_ms=elapsed,
            )
        )
        if len(samples) < 5:
            samples.append(summarize_response("market-status", cache_key, response))
        count += 1
        if count % 500 == 0:
            log(f"[market-status] built {count:,} rows")
    if not dry_run:
        insert_response_rows(rows, batch_size=batch_size)
    return {"rows": len(rows), "samples": samples}


def iter_brand_batches(view_type: str, batch_size: int) -> Iterable[list[dict[str, Any]]]:
    mart = BRAND_MARTS[view_type]["brand_mart"]
    last_id = 0
    while True:
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT *
                    FROM {mart}
                    WHERE id > %s
                    ORDER BY id
                    LIMIT %s
                    """,
                    (last_id, batch_size),
                )
                rows = list(cur.fetchall())
        if not rows:
            break
        last_id = int(rows[-1]["id"])
        yield rows


def rebuild_brand_endpoint_cache(
    endpoint: str,
    markets: dict[tuple[str, str | None, str | None, str | None], dict[str, Any]],
    *,
    dry_run: bool = False,
    limit: int | None = None,
    batch_size: int = 500,
) -> dict[str, Any]:
    if endpoint not in {"cause", "deep-analysis"}:
        raise ValueError(endpoint)
    total = 0
    samples: list[dict[str, Any]] = []
    by_view: dict[str, int] = {}
    pending: list[dict[str, Any]] = []

    for view_type in BRAND_MARTS:
        log(f"[{endpoint}] rebuilding view={view_type}")
        for batch in iter_brand_batches(view_type, batch_size):
            for brand_row in batch:
                if limit is not None and total >= limit:
                    break
                market_id = market_id_for_brand_row(view_type, brand_row)
                source = brand_row.get("source")
                measure = brand_row.get("measure")
                market_row = markets.get(market_key(view_type, market_id, source, measure))
                start = time.perf_counter()
                if endpoint == "cause":
                    response = build_cause_response_from_rows(view_type, brand_row, market_row)
                else:
                    response = build_deep_analysis_response_from_rows(view_type, brand_row, market_row)
                elapsed = int((time.perf_counter() - start) * 1000)
                missing = validate_response(endpoint, response)
                if missing:
                    raise RuntimeError(
                        f"{endpoint} {view_type}/{brand_row.get('brand_key')}/{source}/{measure}: missing {missing}"
                    )
                cache_key = (
                    f"endpoint={endpoint}|view={view_type}|brand_key={brand_row.get('brand_key')}"
                    f"|market_id={market_id}|source={source}|measure={measure}"
                )
                pending.append(
                    response_row(
                        endpoint=endpoint,
                        cache_key=cache_key,
                        response=response,
                        view_type=view_type,
                        source=source,
                        measure=measure,
                        brand_name=brand_row.get("brand_name"),
                        brand_key=brand_row.get("brand_key"),
                        market_id=market_id,
                        payload={
                            "builder_version": BUILDER_VERSION,
                            "source_mart": BRAND_MARTS[view_type]["brand_mart"],
                            "market_mart": BRAND_MARTS[view_type]["market_mart"],
                            "computed_at": now_iso(),
                        },
                        computation_ms=elapsed,
                    )
                )
                if len(samples) < 5:
                    samples.append(summarize_response(endpoint, cache_key, response))
                total += 1
                by_view[view_type] = by_view.get(view_type, 0) + 1
                if len(pending) >= batch_size:
                    if not dry_run:
                        insert_response_rows(pending, batch_size=batch_size)
                    pending.clear()
                if total % 1000 == 0:
                    log(f"[{endpoint}] built {total:,} rows")
            if limit is not None and total >= limit:
                break
        if limit is not None and total >= limit:
            break
    if pending and not dry_run:
        insert_response_rows(pending, batch_size=batch_size)
    return {"rows": total, "by_view": by_view, "samples": samples}


def rebuild_cache(args: argparse.Namespace) -> dict[str, Any]:
    if args.truncate_first and not args.dry_run:
        truncate_response_store()

    endpoints = set(args.endpoints.split(","))
    result: dict[str, Any] = {
        "started_at": now_iso(),
        "dry_run": args.dry_run,
        "endpoints": sorted(endpoints),
    }
    markets = load_market_rows()
    result["market_rows_available"] = len(markets)

    if "brands" in endpoints:
        result["brands"] = rebuild_brands_cache(dry_run=args.dry_run)
    if "market-status" in endpoints:
        result["market-status"] = rebuild_market_status_cache(
            markets,
            dry_run=args.dry_run,
            limit=args.limit_market_rows,
            batch_size=args.batch_size,
        )
    if "cause" in endpoints:
        result["cause"] = rebuild_brand_endpoint_cache(
            "cause",
            markets,
            dry_run=args.dry_run,
            limit=args.limit_brand_rows,
            batch_size=args.batch_size,
        )
    if "deep-analysis" in endpoints:
        result["deep-analysis"] = rebuild_brand_endpoint_cache(
            "deep-analysis",
            markets,
            dry_run=args.dry_run,
            limit=args.limit_brand_rows,
            batch_size=args.batch_size,
        )
    result["finished_at"] = now_iso()
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--truncate-first", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--batch-size", type=int, default=250)
    parser.add_argument("--limit-brand-rows", type=int, default=None)
    parser.add_argument("--limit-market-rows", type=int, default=None)
    parser.add_argument(
        "--endpoints",
        default="brands,market-status,cause,deep-analysis",
        help="Comma-separated subset: brands,market-status,cause,deep-analysis",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = rebuild_cache(args)
    print(json_dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
