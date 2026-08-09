#!/usr/bin/env python3
"""Build all general and strategic forecasts into isolated staging tables."""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
import json
import math
import os
from typing import Any, Final, Mapping, Sequence, TypeVar

from pipeline.scripts.etl import build_cache_deep_analysis_general as general_builder
from pipeline.scripts.etl.cache_build_common import mariadb_connect
from pipeline.scripts.etl.ops_forecast_payload import build_scope_rows
from pipeline.scripts.etl.ops_forecast_scope import Scope, Unit, load_scope, load_units
from pipeline.scripts.etl.ops_forecast_store import (
    LIVE_BLOCK,
    LIVE_HORIZON,
    completion_gate,
    contamination_count,
    epoch_is_current,
    existing_block_keys,
    existing_horizon_keys,
    insert_blocks,
    insert_horizons,
    mart_source_epoch,
    prepare_staging,
    prepare_scoped_staging,
    upsert_blocks,
    upsert_horizons,
)

EXPECTED_BLOCKS: Final[int] = 43_790
EXPECTED_HORIZONS: Final[int] = 3_002
DEFAULT_BLOCK_STAGE: Final[str] = "deep_forecast_block_stage_ops_20260713"
DEFAULT_HORIZON_STAGE: Final[str] = "deep_forecast_horizon_stage_ops_20260713"
RUNTIME_PINS: Final[dict[str, str]] = {
    "NPY_DISABLE_CPU_FEATURES": "X86_V3,X86_V4",
    "OPENBLAS_CORETYPE": "Nehalem",
    "OMP_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
}
T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class RuntimePinError(Exception):
    key: str
    expected: str
    actual: str | None

    def __str__(self) -> str:
        return f"runtime pin mismatch: {self.key} expected={self.expected!r} actual={self.actual!r}"


def assert_runtime_pins(environment: Mapping[str, str]) -> None:
    for key, expected in RUNTIME_PINS.items():
        if environment.get(key) != expected:
            raise RuntimePinError(key, expected, environment.get(key))


def stride_order(values: Sequence[T], stride: int) -> list[T]:
    if not values:
        return []
    if stride <= 0:
        raise ValueError("stride must be positive")
    ordered: list[T] = []
    seen: set[int] = set()
    index = 0
    while index not in seen:
        seen.add(index)
        ordered.append(values[index])
        index = (index + stride) % len(values)
    if len(ordered) != len(values):
        raise ValueError("stride must be coprime with sequence length")
    return ordered


def _generated_at(connection: Any, block_table: str) -> datetime:
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT MIN(generated_at) AS generated_at FROM `{block_table}`")
        row = cursor.fetchone()
    return row["generated_at"] or datetime.now().replace(microsecond=0)


def _group_units(units: list[Unit]) -> dict[Scope, list[Unit]]:
    grouped: dict[Scope, list[Unit]] = defaultdict(list)
    for unit in units:
        grouped[unit.scope].append(unit)
    return grouped


def filter_units(
    units: list[Unit],
    *,
    source: str,
    market_ids: tuple[str, ...],
    brand_keys: tuple[str, ...],
) -> list[Unit]:
    """Select complete forecast scopes touched by one source publication."""
    source_units = [unit for unit in units if unit.scope.source == source]
    markets = set(market_ids)
    brands = set(brand_keys)
    selected_scopes = {
        unit.scope
        for unit in source_units
        if (
            unit.scope.view_kind == "general"
            and (not markets or unit.scope.market_id in markets)
        )
        or (
            unit.scope.view_kind != "general"
            and (not brands or unit.brand_key in brands)
        )
    }
    return [unit for unit in source_units if unit.scope in selected_scopes]


def verify_stride_replay(connection: Any, table: str, units: list[Unit], *, workers: int, sample_size: int) -> dict[str, int]:
    sample = units[:: max(1, len(units) // sample_size)][:sample_size]
    if not sample:
        raise RuntimeError("stride replay requires at least one forecast unit")
    stride = 1 if len(sample) == 1 else next(
        value
        for value in range(max(2, len(sample) // 3), len(sample))
        if math.gcd(value, len(sample)) == 1
    )
    replay = stride_order(sample, stride)
    stored: dict[tuple[str, str, str], dict[str, Any]] = {}
    with connection.cursor() as cursor:
        for unit in replay:
            cursor.execute(
                f"SELECT forecast_json, simulation_json, source_epoch, generated_at FROM `{table}` "
                "WHERE brand_key = %s AND source = %s AND market_id = %s",
                unit.key,
            )
            stored[unit.key] = cursor.fetchone()
    groups = _group_units(replay)
    mismatched = 0
    for scope, scope_units in reversed(list(groups.items())):
        native_rows = load_scope(connection, scope)
        reference = stored[scope_units[0].key]
        rebuilt, _horizons = build_scope_rows(
            scope,
            list(reversed(scope_units)),
            native_rows,
            workers=workers,
            source_epoch=str(reference["source_epoch"]),
            generated_at=reference["generated_at"],
        )
        for row in rebuilt:
            expected = stored[(row.brand_key, row.source, row.market_id)]
            if row.forecast_json != str(expected["forecast_json"]) or row.simulation_json != (
                str(expected["simulation_json"]) if expected["simulation_json"] is not None else None
            ):
                mismatched += 1
    if mismatched:
        raise RuntimeError(f"stride replay gate failed: {mismatched}/{len(sample)}")
    return {"sampled": len(sample), "byte_identical": len(sample), "stride": stride}


def run(args: argparse.Namespace) -> None:
    assert_runtime_pins(os.environ)
    connection = mariadb_connect()
    try:
        general_builder.assert_d2_database(connection)
        epoch = mart_source_epoch(connection)
        all_units = load_units(connection)
        if len(all_units) != args.expected_blocks:
            raise RuntimeError(f"unit count changed: expected={args.expected_blocks} actual={len(all_units)}")
        scope_source = getattr(args, "scope_source", None)
        scope_market_ids = tuple(getattr(args, "scope_market_id", ()) or ())
        scope_brand_keys = tuple(getattr(args, "brand", ()) or ())
        if (scope_market_ids or scope_brand_keys) and scope_source is None:
            raise RuntimeError("scope markets/brands require --scope-source")
        scoped = scope_source is not None
        units = (
            filter_units(
                all_units,
                source=scope_source,
                market_ids=scope_market_ids,
                brand_keys=scope_brand_keys,
            )
            if scoped
            else all_units
        )
        if not units:
            raise RuntimeError("affected scope resolved to zero forecast units")
        if not scoped and not args.force and epoch_is_current(connection, LIVE_BLOCK, epoch, args.expected_blocks) and epoch_is_current(connection, LIVE_HORIZON, epoch, args.expected_horizons):
            print(json.dumps({"stage": "no_op", "source_epoch": epoch, "blocks": len(all_units)}), flush=True)
            return
        if scoped:
            prepare_scoped_staging(
                connection,
                args.block_table,
                args.horizon_table,
                args.expected_blocks,
                args.expected_horizons,
            )
            done_blocks: set[tuple[str, str, str]] = set()
            done_horizons: set[tuple[str, str, str]] = set()
            reused_blocks = args.expected_blocks - len(units)
            reused_horizons = args.expected_horizons
            generated_at = datetime.now().replace(microsecond=0)
        else:
            prepare_staging(connection, args.block_table, args.horizon_table, epoch)
            done_blocks = existing_block_keys(connection, args.block_table)
            done_horizons = existing_horizon_keys(connection, args.horizon_table)
            reused_blocks = len(done_blocks)
            reused_horizons = len(done_horizons)
            generated_at = _generated_at(connection, args.block_table)
        groups = _group_units(units)
        source_scope_count = (
            len(_group_units([unit for unit in all_units if unit.scope.source == scope_source]))
            if scoped
            else len(groups)
        )
        if scoped:
            print(
                json.dumps(
                    {
                        "stage": "scope_selected",
                        "source": scope_source,
                        "scope_count": len(groups),
                        "source_scope_upper_bound": source_scope_count,
                        "block_count": len(units),
                        "global_block_count": len(all_units),
                    }
                ),
                flush=True,
            )
        scope_build_calls = 0
        inserted_blocks = 0
        inserted_horizons = 0
        for index, (scope, scope_units) in enumerate(sorted(groups.items(), key=lambda item: (item[0].view_kind, item[0].market_id, item[0].source)), start=1):
            pending = scope_units if scoped else [unit for unit in scope_units if unit.key not in done_blocks]
            missing_horizon = scoped or not all((scope.market_id, scope.source, measure) in done_horizons for measure in _measures(scope.source))
            if not pending and not missing_horizon:
                continue
            scope_build_calls += 1
            native_rows = load_scope(connection, scope)
            blocks, horizons = build_scope_rows(scope, pending or scope_units[:1], native_rows, workers=args.workers, source_epoch=epoch, generated_at=generated_at)
            if not pending:
                blocks = []
            horizons = [row for row in horizons if (row.market_id, row.source, row.measure) not in done_horizons]
            if scoped:
                inserted_blocks += upsert_blocks(connection, args.block_table, blocks)
                inserted_horizons += upsert_horizons(connection, args.horizon_table, horizons)
                reused_horizons -= len(horizons)
            else:
                inserted_blocks += insert_blocks(connection, args.block_table, blocks)
                inserted_horizons += insert_horizons(connection, args.horizon_table, horizons)
            done_blocks.update((row.brand_key, row.source, row.market_id) for row in blocks)
            done_horizons.update((row.market_id, row.source, row.measure) for row in horizons)
            print(json.dumps({"stage": "scope_done", "index": index, "scopes": len(groups), "scope": [scope.view_kind, scope.market_id, scope.source], "blocks_total": inserted_blocks, "horizons_total": inserted_horizons}), flush=True)
        counts = completion_gate(connection, args.block_table, args.horizon_table, args.expected_blocks, args.expected_horizons)
        replay = verify_stride_replay(
            connection,
            args.block_table,
            units,
            workers=args.workers,
            sample_size=args.verify_sample,
        )
        contaminated = contamination_count(connection, args.block_table)
        if contaminated:
            raise RuntimeError(f"native scope contamination gate failed: {contaminated}")
        print(json.dumps({"stage": "complete", "source_epoch": epoch, **counts, "scope_build_calls": scope_build_calls, "inserted_blocks": inserted_blocks, "inserted_horizons": inserted_horizons, "reused_blocks": reused_blocks, "reused_horizons": reused_horizons, "stride_replay": replay, "contamination": contaminated, "generated_at": generated_at.isoformat(timespec="seconds")}), flush=True)
    finally:
        connection.close()


def _measures(source: str) -> tuple[str, ...]:
    return ("sales", "volume") if source == "ubist" else ("counting_unit", "dosage_unit", "sales", "unit")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--block-table", default=DEFAULT_BLOCK_STAGE)
    parser.add_argument("--horizon-table", default=DEFAULT_HORIZON_STAGE)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--expected-blocks", type=int, default=EXPECTED_BLOCKS)
    parser.add_argument("--expected-horizons", type=int, default=EXPECTED_HORIZONS)
    parser.add_argument("--verify-sample", type=int, default=100)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--scope-source", choices=("ubist", "iqvia_nsa"))
    parser.add_argument("--scope-market-id", action="append", default=[])
    parser.add_argument("--brand", action="append", default=[])
    return parser.parse_args()


def main() -> None:
    general_builder.apply_api_db_env_fallback()
    run(parse_args())


if __name__ == "__main__":
    main()
