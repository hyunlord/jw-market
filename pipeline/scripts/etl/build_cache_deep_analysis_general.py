#!/usr/bin/env python3
"""Build general-view deep-analysis forecast cache from mart_general_brand_metric."""

from __future__ import annotations

import argparse
import copy
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN
import math
import os
from pathlib import Path
import sys
from typing import Any, Final

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_DIR = Path(__file__).resolve().parent
for path in (PROJECT_ROOT, SCRIPT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from cache_build_common import api_source, decode_json, dump_payload, mariadb_connect, metric_recent, parser
from pipeline.scripts.etl.build_cache_deep_analysis import (
    ALL_COMBOS,
    FORECAST_DISCLOSURE,
    FORECAST_METHOD,
    SOURCE_TO_INTERNAL,
    UNIT_LABELS,
    _attach_forecast_ms_series,
    top6_rows,
)
from pipeline.scripts.etl.cache_deep_analysis_brand_factors import dump_brand_factors, load_brand_factor_map
from pipeline.scripts.forecast.forecast_runner import (
    build_forecast_brand_entry,
    build_market_forecast,
    build_simulation_combo,
    forecast_periods_from_history,
    forecast_steps,
    history_from_row,
)

TARGET_DATABASE: Final[str] = "jw_mart_d2_stage_20260630_r2"
GENERAL_CACHE_TABLE: Final[str] = "cache_deep_analysis_general"
GENERAL_BRAND_TABLE: Final[str] = "mart_general_brand_metric"


@dataclass(frozen=True, slots=True)
class GeneralCacheRow:
    brand_key: str
    brand: str
    atc4_code: str
    market_id: str
    response_json: str
    payload_size: int
    brand_factors: str


GroupKey = tuple[str, str]
ComboKey = tuple[str, str, str]


def quote_ident(name: str) -> str:
    if not name.replace("_", "").isalnum():
        raise SystemExit(f"unsafe identifier: {name!r}")
    return "`" + name.replace("`", "``") + "`"


def apply_api_db_env_fallback() -> None:
    fallback_pairs = {
        "MARIADB_HOST": "DB_HOST",
        "MARIADB_PORT": "DB_PORT",
        "MARIADB_DATABASE": "DB_NAME",
        "MARIADB_USER": "DB_USER",
        "MARIADB_PASSWORD": "DB_PASSWORD",
    }
    for mariadb_key, api_key in fallback_pairs.items():
        if not os.environ.get(mariadb_key) and os.environ.get(api_key):
            os.environ[mariadb_key] = os.environ[api_key]


def ensure_general_cache_table(conn: Any, table_name: str = GENERAL_CACHE_TABLE) -> None:
    table = quote_ident(table_name)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {table} (
                brand_key VARCHAR(255) NOT NULL,
                brand VARCHAR(255) NOT NULL,
                atc4_code VARCHAR(16) NOT NULL,
                market_id VARCHAR(32) NOT NULL,
                response_json LONGTEXT NOT NULL CHECK (JSON_VALID(response_json)),
                payload_size INT NOT NULL,
                brand_factors LONGTEXT NULL CHECK (brand_factors IS NULL OR JSON_VALID(brand_factors)),
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                    ON UPDATE CURRENT_TIMESTAMP,
                PRIMARY KEY (brand_key, atc4_code),
                INDEX idx_cache_deep_general_brand (brand),
                INDEX idx_cache_deep_general_atc4 (atc4_code),
                INDEX idx_cache_deep_general_market (market_id)
            ) CHARACTER SET utf8mb4 COLLATE utf8mb4_uca1400_ai_ci
            """
        )


def assert_d2_database(conn: Any) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT DATABASE() AS db")
        row = cur.fetchone()
    current = str(row.get("db") if isinstance(row, dict) else "")
    if current != TARGET_DATABASE:
        raise SystemExit(f"refusing to write non-d2 database: {current}")


def fetch_general_rows(conn: Any) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT *
            FROM {quote_ident(GENERAL_BRAND_TABLE)}
            WHERE NULLIF(brand_key, '') IS NOT NULL
              AND NULLIF(brand_name, '') IS NOT NULL
              AND NULLIF(atc4_code, '') IS NOT NULL
            ORDER BY brand_key, atc4_code, source, measure
            """
        )
        return list(cur.fetchall())


def select_group_keys(
    conn: Any,
    *,
    brands: set[str] | None,
    atc4: str | None,
    limit_groups: int | None,
) -> list[GroupKey]:
    where = [
        "NULLIF(brand_key, '') IS NOT NULL",
        "NULLIF(brand_name, '') IS NOT NULL",
        "NULLIF(atc4_code, '') IS NOT NULL",
    ]
    params: list[Any] = []
    if brands:
        placeholders = ", ".join(["%s"] * len(brands))
        where.append(f"(brand_key IN ({placeholders}) OR brand_name IN ({placeholders}))")
        ordered_brands = sorted(brands)
        params.extend(ordered_brands)
        params.extend(ordered_brands)
    if atc4:
        where.append("atc4_code = %s")
        params.append(atc4)
    limit_sql = ""
    if limit_groups is not None:
        limit_sql = " LIMIT %s"
        params.append(limit_groups)
    sql = f"""
        SELECT brand_key, atc4_code
        FROM {quote_ident(GENERAL_BRAND_TABLE)}
        WHERE {" AND ".join(where)}
        GROUP BY brand_key, atc4_code
        ORDER BY brand_key, atc4_code
        {limit_sql}
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [(str(row["brand_key"]), str(row["atc4_code"])) for row in cur.fetchall()]


def chunked(items: list[GroupKey], size: int) -> list[list[GroupKey]]:
    return [items[start : start + size] for start in range(0, len(items), max(1, size))]


def fetch_rows_for_groups(conn: Any, group_keys: list[GroupKey]) -> list[dict[str, Any]]:
    if not group_keys:
        return []
    pairs = ", ".join(["(%s, %s)"] * len(group_keys))
    params = [value for key in group_keys for value in key]
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT *
            FROM {quote_ident(GENERAL_BRAND_TABLE)}
            WHERE (brand_key, atc4_code) IN ({pairs})
            ORDER BY brand_key, atc4_code, source, measure
            """,
            params,
        )
        return list(cur.fetchall())


def fetch_market_rows_for_atc4s(conn: Any, atc4_codes: set[str]) -> list[dict[str, Any]]:
    if not atc4_codes:
        return []
    placeholders = ", ".join(["%s"] * len(atc4_codes))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT *
            FROM {quote_ident(GENERAL_BRAND_TABLE)}
            WHERE atc4_code IN ({placeholders})
            ORDER BY atc4_code, source, measure, brand_name
            """,
            sorted(atc4_codes),
        )
        return list(cur.fetchall())


def choose_base(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return sorted(rows, key=lambda row: (str(row.get("brand_name") or ""), str(row.get("source") or ""), str(row.get("measure") or "")))[0]


def row_identity(row: dict[str, Any]) -> str:
    return str(row.get("id") or f"{row.get('brand_key')}|{row.get('atc4_code')}|{row.get('source')}|{row.get('measure')}")


def entry_for_target(entry: dict[str, Any], target_brand: str) -> dict[str, Any]:
    payload = copy.deepcopy(entry)
    payload["is_target"] = payload.get("brand") == target_brand
    return payload


def combo_payload(
    row: dict[str, Any],
    *,
    market_forecast: dict[str, Any],
    selected_entries: list[dict[str, Any]],
    target_brand: str,
    source: str,
) -> dict[str, Any]:
    periods, _values = history_from_row(row)
    steps = forecast_steps(source)
    brand_entries = [entry_for_target(entry, target_brand) for entry in selected_entries]
    if not brand_entries:
        brand_entries = [
            build_forecast_brand_entry(row, target_brand=target_brand, source=source, measure=str(row.get("measure")), forecast_steps_count=steps)
        ]
    payload = {
        "period_unit": "월" if SOURCE_TO_INTERNAL[source] == "ubist" else "분기",
        "unit_label": row.get("unit_label") or UNIT_LABELS.get(str(row.get("measure"))),
        "history_periods": periods,
        "forecast_periods": forecast_periods_from_history(periods, source, steps),
        "target_brand": row.get("brand_name"),
        "brands": brand_entries,
        "baseline": {
            "value_recent": metric_recent(decode_json(row.get("metric_history"))).get("raw_value"),
            "ms_recent_pct": metric_recent(decode_json(row.get("metric_history"))).get("ms"),
        },
        "_market_forecast": market_forecast,
    }
    return _attach_forecast_ms_series(payload, market_forecast=market_forecast)


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [json_safe(item) for item in value]
    if hasattr(value, "item"):
        return json_safe(value.item())
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        try:
            Decimal(str(value)).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
        except (InvalidOperation, ValueError, OverflowError):
            return None
    return value


def build_general_cache_row(
    group_key: tuple[str, str],
    brand_rows: list[dict[str, Any]],
    *,
    market_forecasts_by_combo: dict[ComboKey, dict[str, Any]],
    selected_entries_by_group_combo: dict[tuple[GroupKey, str], list[dict[str, Any]]],
    brand_factors_by_brand: dict[str, dict[str, Any]],
) -> GeneralCacheRow:
    brand_key, atc4_code = group_key
    base = choose_base(brand_rows)
    brand = str(base.get("brand_name") or brand_key)
    market_id = f"general:{atc4_code}"
    rows_by_combo = {f"{api_source(row['source'])}.{row['measure']}": row for row in brand_rows}
    by_combo: dict[str, Any] = {}
    simulation_by_combo: dict[str, Any] = {}
    for source, measure in ALL_COMBOS:
        combo = f"{source}.{measure}"
        row = rows_by_combo.get(combo)
        if row is None:
            continue
        internal_source = SOURCE_TO_INTERNAL[source]
        combo_key = (atc4_code, internal_source, measure)
        market_forecast = market_forecasts_by_combo.get(combo_key, {"history_periods": [], "history_values": [], "forecast_values": []})
        selected_entries = selected_entries_by_group_combo.get((group_key, combo), [])
        combo_data = combo_payload(row, market_forecast=market_forecast, selected_entries=selected_entries, target_brand=brand, source=source)
        combo_data.pop("_market_forecast", None)
        by_combo[combo] = combo_data
        simulation_by_combo[combo] = build_simulation_combo(
            combo=combo,
            source=source,
            measure=measure,
            unit_label=combo_data.get("unit_label"),
            forecast_combo=combo_data,
            market_forecast=market_forecast,
            cut_b_events=[],
        )

    payload = {
        "brand": brand,
        "brand_name": brand,
        "brand_key": brand_key,
        "market_id": market_id,
        "market_name": base.get("atc4_desc"),
        "available_combos": sorted(by_combo),
        "data": {
            "forecast": {
                "method": FORECAST_METHOD,
                "disclaimer": FORECAST_DISCLOSURE,
                "is_statistical_model": True,
                "backtest_available": True,
                "event_regressor_enabled": False,
                "phase29_poc": None,
                "by_combo": by_combo,
            },
            "simulation": {"by_combo": simulation_by_combo},
            "events": [],
        },
        "market_meta": {
            "market_name": base.get("atc4_desc"),
            "atc4_code": atc4_code,
            "atc4_name": base.get("atc4_desc"),
            "sources": sorted({api_source(row["source"]) for row in brand_rows}),
            "default_source": api_source(base.get("source")),
            "available_combos": sorted(by_combo),
            "source_count": len({row["source"] for row in brand_rows}),
            "measure_count": len({row["measure"] for row in brand_rows}),
            "market_count": 1,
            "is_jw": bool(base.get("is_jw")),
            "is_target": bool(base.get("is_target")),
            "cache_scope": "general",
            "tie_break": "brand_atc4_exact_or_atc4_ascending",
        },
    }
    safe_payload = json_safe(payload)
    response_json = dump_payload(safe_payload)
    return GeneralCacheRow(
        brand_key=brand_key,
        brand=brand,
        atc4_code=atc4_code,
        market_id=market_id,
        response_json=response_json,
        payload_size=len(response_json.encode("utf-8")),
        brand_factors=dump_brand_factors(brand_factors_by_brand.get(brand)),
    )


def select_groups(
    grouped: dict[tuple[str, str], list[dict[str, Any]]],
    *,
    brands: set[str] | None,
    atc4: str | None,
    limit_groups: int | None,
) -> list[tuple[tuple[str, str], list[dict[str, Any]]]]:
    items = sorted(grouped.items(), key=lambda item: (item[0][0], item[0][1]))
    if brands is not None:
        items = [item for item in items if str(choose_base(item[1]).get("brand_name") or "") in brands or item[0][0] in brands]
    if atc4 is not None:
        items = [item for item in items if item[0][1] == atc4]
    return items[:limit_groups] if limit_groups is not None else items


def build_market_forecasts_by_combo(
    market_rows_by_combo: dict[ComboKey, list[dict[str, Any]]],
    *,
    workers: int,
) -> dict[ComboKey, dict[str, Any]]:
    forecasts: dict[ComboKey, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as executor:
        futures = {
            executor.submit(build_market_forecast, rows, api_source(source), forecast_steps(api_source(source))): combo_key
            for combo_key, rows in sorted(market_rows_by_combo.items())
            for _atc4_code, source, _measure in [combo_key]
        }
        for future in as_completed(futures):
            forecasts[futures[future]] = future.result()
    return forecasts


def select_entries_for_groups(
    grouped: dict[GroupKey, list[dict[str, Any]]],
    market_rows_by_combo: dict[ComboKey, list[dict[str, Any]]],
) -> tuple[dict[tuple[GroupKey, str], list[str]], dict[tuple[ComboKey, str], dict[str, Any]]]:
    selected_ids_by_group_combo: dict[tuple[GroupKey, str], list[str]] = {}
    entry_rows: dict[tuple[ComboKey, str], dict[str, Any]] = {}
    for group_key, brand_rows in sorted(grouped.items(), key=lambda item: (item[0][0], item[0][1])):
        brand_key, atc4_code = group_key
        brand = str(choose_base(brand_rows).get("brand_name") or brand_key)
        rows_by_combo = {f"{api_source(row['source'])}.{row['measure']}": row for row in brand_rows}
        for source, measure in ALL_COMBOS:
            combo = f"{source}.{measure}"
            row = rows_by_combo.get(combo)
            if row is None:
                continue
            combo_key = (atc4_code, SOURCE_TO_INTERNAL[source], measure)
            selected = top6_rows(market_rows_by_combo.get(combo_key, []), brand)
            if not selected:
                selected = [row]
            selected_ids: list[str] = []
            for selected_row in selected:
                identity = row_identity(selected_row)
                selected_ids.append(identity)
                entry_rows[(combo_key, identity)] = selected_row
            selected_ids_by_group_combo[(group_key, combo)] = selected_ids
    return selected_ids_by_group_combo, entry_rows


def build_forecast_entries_by_combo(
    entry_rows: dict[tuple[ComboKey, str], dict[str, Any]],
    *,
    workers: int,
) -> dict[tuple[ComboKey, str], dict[str, Any]]:
    entries: dict[tuple[ComboKey, str], dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as executor:
        futures = {
            executor.submit(
                build_forecast_brand_entry,
                row,
                target_brand="",
                source=api_source(combo_key[1]),
                measure=combo_key[2],
                forecast_steps_count=forecast_steps(api_source(combo_key[1])),
            ): entry_key
            for entry_key, row in sorted(entry_rows.items())
            for combo_key, _identity in [entry_key]
        }
        for future in as_completed(futures):
            entries[futures[future]] = future.result()
    return entries


def selected_entries_for_payload(
    selected_ids_by_group_combo: dict[tuple[GroupKey, str], list[str]],
    entries_by_combo: dict[tuple[ComboKey, str], dict[str, Any]],
    group_key: GroupKey,
    combo: str,
    combo_key: ComboKey,
) -> list[dict[str, Any]]:
    selected_ids = selected_ids_by_group_combo.get((group_key, combo), [])
    return [entries_by_combo[(combo_key, identity)] for identity in selected_ids if (combo_key, identity) in entries_by_combo]


def build_batch_rows(
    conn: Any,
    group_keys: list[GroupKey],
    *,
    workers: int,
    verbose: bool,
) -> list[GeneralCacheRow]:
    brand_rows = fetch_rows_for_groups(conn, group_keys)
    grouped: dict[GroupKey, list[dict[str, Any]]] = defaultdict(list)
    for row in brand_rows:
        grouped[(str(row["brand_key"]), str(row["atc4_code"]))].append(row)

    market_rows_by_combo: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in fetch_market_rows_for_atc4s(conn, {atc4_code for _brand_key, atc4_code in group_keys}):
        market_rows_by_combo[(str(row["atc4_code"]), str(row["source"]), str(row["measure"]))].append(row)

    market_forecasts = build_market_forecasts_by_combo(market_rows_by_combo, workers=workers)
    selected_ids_by_group_combo, entry_rows = select_entries_for_groups(grouped, market_rows_by_combo)
    entries_by_combo = build_forecast_entries_by_combo(entry_rows, workers=workers)
    selected_entries_by_group_combo: dict[tuple[GroupKey, str], list[dict[str, Any]]] = {}
    for group_key, group_rows in grouped.items():
        atc4_code = group_key[1]
        rows_by_combo = {f"{api_source(row['source'])}.{row['measure']}": row for row in group_rows}
        for source, measure in ALL_COMBOS:
            combo = f"{source}.{measure}"
            if combo not in rows_by_combo:
                continue
            combo_key = (atc4_code, SOURCE_TO_INTERNAL[source], measure)
            selected_entries_by_group_combo[(group_key, combo)] = selected_entries_for_payload(
                selected_ids_by_group_combo,
                entries_by_combo,
                group_key,
                combo,
                combo_key,
            )

    selected_brands = sorted({str(choose_base(rows).get("brand_name") or key[0]) for key, rows in grouped.items()})
    brand_factors = load_brand_factor_map(conn, selected_brands)
    built: list[GeneralCacheRow] = []
    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as executor:
        futures = [
            executor.submit(
                build_general_cache_row,
                group_key,
                group_rows,
                market_forecasts_by_combo=market_forecasts,
                selected_entries_by_group_combo=selected_entries_by_group_combo,
                brand_factors_by_brand=brand_factors,
            )
            for group_key, group_rows in sorted(grouped.items(), key=lambda item: (item[0][0], item[0][1]))
        ]
        for index, future in enumerate(as_completed(futures), start=1):
            built.append(future.result())
            if verbose and index % 100 == 0:
                print(f"built batch cache_deep_analysis_general rows={index}/{len(futures)}", flush=True)
    return sorted(built, key=lambda row: (row.brand_key, row.atc4_code))


def write_rows(conn: Any, rows: list[GeneralCacheRow], *, table_name: str, batch_size: int) -> None:
    columns = ["brand_key", "brand", "atc4_code", "market_id", "response_json", "payload_size", "brand_factors"]
    placeholders = ", ".join(["%s"] * len(columns))
    names = ", ".join(f"`{column}`" for column in columns)
    updates = ", ".join(
        f"`{column}` = VALUES(`{column}`)"
        for column in columns
        if column not in {"brand_key", "atc4_code"}
    )
    sql = (
        f"INSERT INTO {quote_ident(table_name)} ({names}) VALUES ({placeholders}) "
        f"ON DUPLICATE KEY UPDATE {updates}"
    )
    with conn.cursor() as cur:
        for start in range(0, len(rows), batch_size):
            batch = rows[start : start + batch_size]
            cur.executemany(sql, [tuple(getattr(row, column) for column in columns) for row in batch])
            conn.commit()


def parse_args() -> argparse.Namespace:
    args_parser = parser(__doc__)
    args_parser.add_argument("--target-table", default=GENERAL_CACHE_TABLE)
    args_parser.add_argument("--workers", type=int, default=4)
    args_parser.add_argument("--batch-size", type=int, default=100)
    args_parser.add_argument("--group-batch-size", type=int, default=100)
    args_parser.add_argument("--limit-groups", type=int)
    args_parser.add_argument("--brand", action="append", dest="brands")
    args_parser.add_argument("--atc4")
    args_parser.add_argument("--dry-run", action="store_true")
    return args_parser.parse_args()


def main() -> None:
    args = parse_args()
    apply_api_db_env_fallback()
    conn = mariadb_connect()
    try:
        assert_d2_database(conn)
        ensure_general_cache_table(conn, args.target_table)
        selected = select_group_keys(conn, brands=set(args.brands) if args.brands else None, atc4=args.atc4, limit_groups=args.limit_groups)
        built_count = 0
        for batch_index, group_batch in enumerate(chunked(selected, int(args.group_batch_size)), start=1):
            built = build_batch_rows(conn, group_batch, workers=args.workers, verbose=args.verbose)
            if not args.dry_run:
                write_rows(conn, built, table_name=args.target_table, batch_size=args.batch_size)
            built_count += len(built)
            if args.verbose:
                print(
                    f"cache_deep_analysis_general batch={batch_index} built={built_count}/{len(selected)} dry_run={args.dry_run}",
                    flush=True,
                )
        if args.verbose:
            print(f"cache_deep_analysis_general built={built_count} dry_run={args.dry_run}", flush=True)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
