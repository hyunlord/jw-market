#!/usr/bin/env python3
"""Generate strategic-view forecast/simulation rows directly into unified serving tables.

Strategic markets (ml_* / cd_*) are computed from their native mart scope
(`mart_strategic_ml_brand_metric` / `mart_strategic_cd_brand_metric`), one
market-source load per group, and written as INSERT-only rows next to the
migrated general-view rows in `deep_forecast_block` / `deep_forecast_horizon`.

Forecast/simulation math is the b8bb46d4 deterministic runner, unchanged: the
market column is aliased to `atc4_code` so the existing per-grain RNG identity
("brand", brand_key, <market_id>, source, measure) extends to strategic grains
without touching pipeline/scripts/forecast/forecast_runner.py. Strategic
market_id values (ml_%/cd_%) never collide with general atc4 codes, so seeds
stay disjoint from the general-view population.

Unlike migrated general rows (updated_at fallback), strategic payloads carry a
producer-recorded generated_at; the runner accepts --generated-at so a
re-computation with the recorded timestamp is byte-identical.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import sys
import time
from typing import Any, Final

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.scripts.etl import build_cache_deep_analysis_general as general_builder
from pipeline.scripts.etl.build_cache_deep_analysis import ALL_COMBOS, FORECAST_DISCLOSURE, FORECAST_METHOD, top6_rows
from pipeline.scripts.etl.cache_build_common import api_source, dump_payload, mariadb_connect
from pipeline.scripts.etl.general_forecast_payload import optimize_and_mark_payload, validate_payload_contract
from pipeline.scripts.forecast.forecast_runner import (
    build_forecast_brand_entry,
    build_market_forecast,
    build_simulation_combo,
    forecast_steps,
)

TARGET_DATABASE: Final[str] = "jw_mart_d2_stage_20260630_r2"
SOURCE_EPOCH: Final[str] = TARGET_DATABASE
BLOCK_TABLE: Final[str] = "deep_forecast_block"
HORIZON_TABLE: Final[str] = "deep_forecast_horizon"
ML_TABLE: Final[str] = "mart_strategic_ml_brand_metric"
CD_TABLE: Final[str] = "mart_strategic_cd_brand_metric"
HORIZON_YEARS: Final[int] = 5
VALID_SOURCES: Final[frozenset[str]] = frozenset({"iqvia_nsa", "ubist"})

UnitKey = tuple[str, str, str]  # (brand_key, source, market_id)
ScopeKey = tuple[str, str, str]  # (view_kind, market_id, mart_source)


@dataclass(frozen=True, slots=True)
class StrategicUnit:
    view_kind: str
    market_id: str
    brand_key: str
    mart_source: str

    @property
    def block_key(self) -> UnitKey:
        return (self.brand_key, self.mart_source, self.market_id)

    @property
    def scope_key(self) -> ScopeKey:
        return (self.view_kind, self.market_id, self.mart_source)


def derive_view_kind(market_id: str) -> str:
    if market_id.startswith("ml_"):
        return "market_landscape"
    if market_id.startswith("cd_"):
        return "competitive_dynamics"
    raise ValueError(f"non-strategic market_id: {market_id!r}")


def _table_spec(view_kind: str) -> tuple[str, str]:
    if view_kind == "market_landscape":
        return ML_TABLE, "ml_id"
    if view_kind == "competitive_dynamics":
        return CD_TABLE, "cd_market_id"
    raise ValueError(f"unsupported strategic view_kind: {view_kind!r}")


def steps_for(source_api: str) -> int:
    return forecast_steps(source_api) * HORIZON_YEARS // 10


def parse_generated_at(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is not None:
        raise SystemExit("--generated-at must be a naive local timestamp")
    return parsed


def load_units(conn: Any) -> list[StrategicUnit]:
    """Mart-defined universe: sales grains of both strategic marts (7,706)."""
    units: list[StrategicUnit] = []
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT 'market_landscape' AS view_kind, ml_id AS market_id, brand_key, source
            FROM {general_builder.quote_ident(ML_TABLE)} WHERE measure = 'sales'
            UNION ALL
            SELECT 'competitive_dynamics', cd_market_id, brand_key, source
            FROM {general_builder.quote_ident(CD_TABLE)} WHERE measure = 'sales'
            """
        )
        for row in cur.fetchall():
            source = str(row["source"])
            if source not in VALID_SOURCES:
                raise SystemExit(f"unsupported mart source: {source!r}")
            units.append(
                StrategicUnit(
                    view_kind=str(row["view_kind"]),
                    market_id=str(row["market_id"]),
                    brand_key=str(row["brand_key"]),
                    mart_source=source,
                )
            )
    units.sort(key=lambda unit: (unit.market_id, unit.mart_source, unit.brand_key))
    if len({unit.block_key for unit in units}) != len(units):
        raise SystemExit("strategic universe contains duplicate block keys")
    return units


def load_units_file(path: Path) -> set[UnitKey]:
    keys: set[UnitKey] = set()
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        parts = line.split("\t")
        if index == 0 and parts[0] == "brand_key":
            continue
        if len(parts) < 3:
            raise SystemExit(f"invalid units line {index + 1}: {line!r}")
        keys.add((parts[0], parts[1], parts[2]))
    return keys


def load_market_scope(conn: Any, scope_key: ScopeKey) -> list[dict[str, Any]]:
    """Load one strategic market-source scope once, across all measures.

    Native-scope lineage (agent3 market_repository.load_native_scope) extended
    from measure='sales' to all measures; the market column is aliased to
    atc4_code so downstream forecast identity/grouping code runs unchanged.
    """
    view_kind, market_id, mart_source = scope_key
    table, market_column = _table_spec(view_kind)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, {market_column} AS atc4_code, brand_key, brand_name, source, measure,
                   unit_label, is_jw, metric_history, computed_at
            FROM {general_builder.quote_ident(table)}
            WHERE {market_column} = %s AND source = %s
            ORDER BY measure, brand_name, brand_key, id
            """,
            (market_id, mart_source),
        )
        rows = list(cur.fetchall())
    if not rows:
        raise RuntimeError(f"empty strategic native scope: {scope_key!r}")
    for row in rows:
        if str(row["atc4_code"]) != market_id or str(row["source"]) != mart_source:
            raise RuntimeError(f"strategic scope contamination: {scope_key!r}")
    return rows


def build_market_products(
    scope_key: ScopeKey,
    scope_rows: list[dict[str, Any]],
    *,
    workers: int,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    view_kind, market_id, mart_source = scope_key
    source_api = api_source(mart_source)
    rows_by_measure: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in scope_rows:
        rows_by_measure[str(row["measure"])].append(row)
    forecasts: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as executor:
        futures = {
            measure: executor.submit(build_market_forecast, rows, source_api, steps_for(source_api))
            for measure, rows in sorted(rows_by_measure.items())
        }
        for measure, future in futures.items():
            forecasts[measure] = future.result()
    return rows_by_measure, forecasts


def build_unit_payload(
    unit: StrategicUnit,
    unit_rows: list[dict[str, Any]],
    *,
    rows_by_measure: dict[str, list[dict[str, Any]]],
    market_forecasts: dict[str, dict[str, Any]],
    generated_at_iso: str,
) -> dict[str, Any]:
    base = general_builder.choose_base(unit_rows)
    brand = str(base.get("brand_name") or unit.brand_key)
    source_api = api_source(unit.mart_source)
    rows_by_combo = {f"{api_source(row['source'])}.{row['measure']}": row for row in unit_rows}
    by_combo: dict[str, Any] = {}
    simulation_by_combo: dict[str, Any] = {}
    for combo_source, measure in ALL_COMBOS:
        if combo_source != source_api:
            continue
        combo = f"{combo_source}.{measure}"
        row = rows_by_combo.get(combo)
        if row is None:
            continue
        steps = steps_for(combo_source)
        market_forecast = market_forecasts.get(measure) or {
            "history_periods": [],
            "history_values": [],
            "forecast_values": [],
        }
        selected = top6_rows(rows_by_measure.get(measure, []), brand) or [row]
        selected_entries = [
            build_forecast_brand_entry(
                selected_row,
                target_brand="",
                source=combo_source,
                measure=measure,
                forecast_steps_count=steps,
            )
            for selected_row in selected
        ]
        combo_data = general_builder.combo_payload(
            row,
            market_forecast=market_forecast,
            selected_entries=selected_entries,
            target_brand=brand,
            source=combo_source,
            forecast_steps_count=steps,
        )
        combo_data.pop("_market_forecast", None)
        by_combo[combo] = combo_data
        simulation_by_combo[combo] = build_simulation_combo(
            combo=combo,
            source=combo_source,
            measure=measure,
            unit_label=combo_data.get("unit_label"),
            forecast_combo=combo_data,
            market_forecast=market_forecast,
            cut_b_events=[],
        )
    payload = {
        "brand": brand,
        "brand_name": brand,
        "brand_key": unit.brand_key,
        "market_id": unit.market_id,
        "view_kind": unit.view_kind,
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
    }
    optimized = optimize_and_mark_payload(general_builder.json_safe(payload))
    validate_payload_contract(optimized)
    optimized["generated_at"] = generated_at_iso
    optimized["timestamp_source"] = "producer"
    return optimized


@dataclass(frozen=True, slots=True)
class BlockValues:
    brand_key: str
    source: str
    market_id: str
    view_kind: str
    forecast_json: str
    simulation_json: str | None
    generation_status: str | None
    no_history_fallback: str | None
    simulation_available: int
    source_epoch: str
    source_computed_at: Any | None
    generated_at: datetime


def block_values_from_payload(
    unit: StrategicUnit,
    payload: dict[str, Any],
    *,
    source_computed_at: Any | None,
    generated_at: datetime,
) -> BlockValues:
    forecast_section = dict(payload["data"]["forecast"])
    forecast_section["generated_at"] = payload["generated_at"]
    forecast_section["timestamp_source"] = "producer"
    availability = payload.get("simulation_available") or {}
    simulation_available = any(bool(value) for value in availability.values())
    simulation_json = dump_payload(payload["data"]["simulation"]) if simulation_available else None
    fallback = payload.get("no_history_fallback")
    if derive_view_kind(unit.market_id) != unit.view_kind:
        raise RuntimeError(f"view_kind mismatch for {unit!r}")
    return BlockValues(
        brand_key=unit.brand_key,
        source=unit.mart_source,
        market_id=unit.market_id,
        view_kind=unit.view_kind,
        forecast_json=dump_payload(forecast_section),
        simulation_json=simulation_json,
        generation_status=str(payload.get("generation_status") or "generated"),
        no_history_fallback=dump_payload(fallback) if isinstance(fallback, dict) else None,
        simulation_available=int(simulation_available),
        source_epoch=SOURCE_EPOCH,
        source_computed_at=source_computed_at,
        generated_at=generated_at,
    )


def source_computed_at(rows: list[dict[str, Any]]) -> Any | None:
    values = [row.get("computed_at") for row in rows if row.get("computed_at") is not None]
    return max(values) if values else None


def existing_block_keys(conn: Any) -> set[UnitKey]:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT brand_key, source, market_id FROM {general_builder.quote_ident(BLOCK_TABLE)} "
            "WHERE market_id LIKE 'ml\\_%' OR market_id LIKE 'cd\\_%'"
        )
        return {(str(row["brand_key"]), str(row["source"]), str(row["market_id"])) for row in cur.fetchall()}


def existing_horizon_keys(conn: Any) -> set[tuple[str, str, str]]:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT market_id, source, measure FROM {general_builder.quote_ident(HORIZON_TABLE)} "
            "WHERE market_id LIKE 'ml\\_%' OR market_id LIKE 'cd\\_%'"
        )
        return {(str(row["market_id"]), str(row["source"]), str(row["measure"])) for row in cur.fetchall()}


def insert_block_rows(conn: Any, rows: list[BlockValues]) -> int:
    if not rows:
        return 0
    sql = (
        f"INSERT INTO {general_builder.quote_ident(BLOCK_TABLE)} ("
        "brand_key, source, market_id, view_kind, forecast_json, simulation_json, "
        "generation_status, no_history_fallback, simulation_available, source_epoch, "
        "source_computed_at, generated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
    )
    values = [
        (
            row.brand_key,
            row.source,
            row.market_id,
            row.view_kind,
            row.forecast_json,
            row.simulation_json,
            row.generation_status,
            row.no_history_fallback,
            row.simulation_available,
            row.source_epoch,
            row.source_computed_at,
            row.generated_at,
        )
        for row in rows
    ]
    with conn.cursor() as cur:
        inserted = cur.executemany(sql, values)
    conn.commit()
    return int(inserted or 0)


def insert_horizon_rows(
    conn: Any,
    scope_key: ScopeKey,
    rows_by_measure: dict[str, list[dict[str, Any]]],
    market_forecasts: dict[str, dict[str, Any]],
    *,
    existing: set[tuple[str, str, str]],
    generated_at_iso: str,
    generated_at: datetime,
) -> int:
    view_kind, market_id, mart_source = scope_key
    sql = (
        f"INSERT INTO {general_builder.quote_ident(HORIZON_TABLE)} ("
        "market_id, source, measure, view_kind, forecast_horizon_json, source_row_count, "
        "source_epoch, source_computed_at, generated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
    )
    values = []
    for measure, forecast in sorted(market_forecasts.items()):
        key = (market_id, mart_source, measure)
        if key in existing:
            continue
        horizon_payload = {
            "history_periods": forecast.get("history_periods") or [],
            "history_values": forecast.get("history_values") or [],
            "forecast_values": forecast.get("forecast_values") or [],
            "generated_at": generated_at_iso,
            "timestamp_source": "producer",
        }
        values.append(
            (
                market_id,
                mart_source,
                measure,
                view_kind,
                dump_payload(general_builder.json_safe(horizon_payload)),
                len(rows_by_measure.get(measure, [])),
                SOURCE_EPOCH,
                source_computed_at(rows_by_measure.get(measure, [])),
                generated_at,
            )
        )
        existing.add(key)
    if not values:
        return 0
    with conn.cursor() as cur:
        inserted = cur.executemany(sql, values)
    conn.commit()
    return int(inserted or 0)


def run_generate(args: argparse.Namespace) -> None:
    generated_at = parse_generated_at(args.generated_at) if args.generated_at else datetime.now().replace(microsecond=0)
    generated_at_iso = generated_at.isoformat(timespec="seconds")
    conn = mariadb_connect()
    try:
        general_builder.assert_d2_database(conn)
        units = load_units(conn)
        if args.units_file:
            requested_keys = load_units_file(Path(args.units_file))
            units = [unit for unit in units if unit.block_key in requested_keys]
            if len(units) != len(requested_keys):
                missing = requested_keys - {unit.block_key for unit in units}
                raise SystemExit(f"units file contains {len(missing)} keys outside mart universe: {sorted(missing)[:5]}")
        if args.limit_units is not None:
            units = units[: args.limit_units]
        done = existing_block_keys(conn)
        pending = [unit for unit in units if unit.block_key not in done]
        horizon_done = existing_horizon_keys(conn)
        print(
            json.dumps(
                {
                    "stage": "start",
                    "universe": len(units),
                    "already_present": len(units) - len(pending),
                    "pending": len(pending),
                    "generated_at": generated_at_iso,
                    "horizon_years": HORIZON_YEARS,
                    "workers": args.workers,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        by_scope: dict[ScopeKey, list[StrategicUnit]] = defaultdict(list)
        for unit in pending:
            by_scope[unit.scope_key].append(unit)
        inserted_blocks = 0
        inserted_horizon = 0
        started = time.monotonic()
        for scope_index, (scope_key, scope_units) in enumerate(sorted(by_scope.items()), start=1):
            scope_started = time.monotonic()
            scope_rows = load_market_scope(conn, scope_key)
            rows_by_measure, market_forecasts = build_market_products(scope_key, scope_rows, workers=args.workers)
            unit_rows_by_brand: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for row in scope_rows:
                unit_rows_by_brand[str(row["brand_key"])].append(row)
            block_rows: list[BlockValues] = []
            computed_at = source_computed_at(scope_rows)
            with ThreadPoolExecutor(max_workers=max(1, int(args.workers))) as executor:
                futures = [
                    (
                        unit,
                        executor.submit(
                            build_unit_payload,
                            unit,
                            unit_rows_by_brand[unit.brand_key],
                            rows_by_measure=rows_by_measure,
                            market_forecasts=market_forecasts,
                            generated_at_iso=generated_at_iso,
                        ),
                    )
                    for unit in sorted(scope_units, key=lambda item: item.brand_key)
                    if unit_rows_by_brand.get(unit.brand_key)
                ]
                missing_scope = [unit.block_key for unit in scope_units if not unit_rows_by_brand.get(unit.brand_key)]
                if missing_scope:
                    raise RuntimeError(f"target absent from strategic scope: {missing_scope[:5]}")
                for unit, future in futures:
                    payload = future.result()
                    block_rows.append(
                        block_values_from_payload(
                            unit,
                            payload,
                            source_computed_at=computed_at,
                            generated_at=generated_at,
                        )
                    )
            if args.dry_run:
                inserted_blocks += len(block_rows)
                inserted_horizon += sum(
                    1
                    for measure in market_forecasts
                    if (scope_key[1], scope_key[2], measure) not in horizon_done
                )
            else:
                for start in range(0, len(block_rows), args.batch_size):
                    inserted_blocks += insert_block_rows(conn, block_rows[start : start + args.batch_size])
                inserted_horizon += insert_horizon_rows(
                    conn,
                    scope_key,
                    rows_by_measure,
                    market_forecasts,
                    existing=horizon_done,
                    generated_at_iso=generated_at_iso,
                    generated_at=generated_at,
                )
            print(
                json.dumps(
                    {
                        "stage": "market_done",
                        "index": scope_index,
                        "markets_total": len(by_scope),
                        "scope": list(scope_key),
                        "scope_rows": len(scope_rows),
                        "units": len(scope_units),
                        "blocks_inserted_total": inserted_blocks,
                        "horizon_inserted_total": inserted_horizon,
                        "market_seconds": round(time.monotonic() - scope_started, 2),
                        "elapsed_seconds": round(time.monotonic() - started, 2),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        strategic_now = existing_block_keys(conn)
        requested = {unit.block_key for unit in units}
        missing = requested - strategic_now
        if args.dry_run:
            print(
                json.dumps(
                    {
                        "stage": "dry_run_done",
                        "requested": len(requested),
                        "built_blocks": inserted_blocks,
                        "built_horizon": inserted_horizon,
                        "wall_seconds": round(time.monotonic() - started, 2),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return
        if missing:
            raise RuntimeError(f"completion gate failed: missing {len(missing)} block rows: {sorted(missing)[:5]}")
        print(
            json.dumps(
                {
                    "stage": "completion_gate",
                    "result": "pass",
                    "requested": len(requested),
                    "strategic_block_rows": len(strategic_now & requested),
                    "blocks_inserted_this_run": inserted_blocks,
                    "horizon_inserted_this_run": inserted_horizon,
                    "wall_seconds": round(time.monotonic() - started, 2),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    finally:
        conn.close()


def run_verify_sample(args: argparse.Namespace) -> None:
    """Recompute a deterministic sample of stored strategic rows and compare bytes."""
    conn = mariadb_connect()
    try:
        general_builder.assert_d2_database(conn)
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT brand_key, source, market_id, view_kind, forecast_json, simulation_json "
                f"FROM {general_builder.quote_ident(BLOCK_TABLE)} "
                "WHERE market_id LIKE 'ml\\_%' OR market_id LIKE 'cd\\_%' "
                "ORDER BY market_id, source, brand_key"
            )
            stored = list(cur.fetchall())
        if not stored:
            raise SystemExit("no strategic rows to verify")
        sample_size = min(args.verify_sample, len(stored))
        stride = max(1, len(stored) // sample_size)
        sampled = stored[::stride][:sample_size]
        by_scope: dict[ScopeKey, list[dict[str, Any]]] = defaultdict(list)
        for row in sampled:
            view_kind = str(row["view_kind"])
            by_scope[(view_kind, str(row["market_id"]), str(row["source"]))].append(row)
        matched = 0
        mismatched: list[dict[str, Any]] = []
        for scope_key, scope_sample in sorted(by_scope.items()):
            scope_rows = load_market_scope(conn, scope_key)
            rows_by_measure, market_forecasts = build_market_products(scope_key, scope_rows, workers=args.workers)
            unit_rows_by_brand: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for row in scope_rows:
                unit_rows_by_brand[str(row["brand_key"])].append(row)
            computed_at = source_computed_at(scope_rows)
            for stored_row in scope_sample:
                unit = StrategicUnit(
                    view_kind=scope_key[0],
                    market_id=scope_key[1],
                    brand_key=str(stored_row["brand_key"]),
                    mart_source=scope_key[2],
                )
                stored_forecast = str(stored_row["forecast_json"])
                stored_generated_at = str(json.loads(stored_forecast).get("generated_at") or "")
                payload = build_unit_payload(
                    unit,
                    unit_rows_by_brand[unit.brand_key],
                    rows_by_measure=rows_by_measure,
                    market_forecasts=market_forecasts,
                    generated_at_iso=stored_generated_at,
                )
                recomputed = block_values_from_payload(
                    unit,
                    payload,
                    source_computed_at=computed_at,
                    generated_at=parse_generated_at(stored_generated_at),
                )
                stored_simulation = stored_row["simulation_json"]
                if recomputed.forecast_json == stored_forecast and recomputed.simulation_json == (
                    str(stored_simulation) if stored_simulation is not None else None
                ):
                    matched += 1
                else:
                    mismatched.append({"key": list(unit.block_key)})
        print(
            json.dumps(
                {
                    "stage": "verify_sample",
                    "sampled": len(sampled),
                    "byte_identical": matched,
                    "mismatched": mismatched[:10],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        if mismatched:
            raise SystemExit(f"self-reproduction failed for {len(mismatched)}/{len(sampled)} sampled units")
    finally:
        conn.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--limit-units", type=int)
    parser.add_argument("--units-file")
    parser.add_argument("--generated-at")
    parser.add_argument("--verify-sample", type=int)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> None:
    general_builder.apply_api_db_env_fallback()
    args = parse_args()
    if args.verify_sample:
        run_verify_sample(args)
    else:
        run_generate(args)


if __name__ == "__main__":
    main()
