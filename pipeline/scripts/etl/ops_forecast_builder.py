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
)

EXPECTED_BLOCKS: Final[int] = 43_790
EXPECTED_HORIZONS: Final[int] = 3_000
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


def verify_stride_replay(connection: Any, table: str, units: list[Unit], *, workers: int, sample_size: int) -> dict[str, int]:
    sample = units[:: max(1, len(units) // sample_size)][:sample_size]
    stride = next(value for value in range(max(2, len(sample) // 3), len(sample)) if math.gcd(value, len(sample)) == 1)
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
        units = load_units(connection)
        if len(units) != args.expected_blocks:
            raise RuntimeError(f"unit count changed: expected={args.expected_blocks} actual={len(units)}")
        if not args.force and epoch_is_current(connection, LIVE_BLOCK, epoch, args.expected_blocks) and epoch_is_current(connection, LIVE_HORIZON, epoch, args.expected_horizons):
            print(json.dumps({"stage": "no_op", "source_epoch": epoch, "blocks": len(units)}), flush=True)
            return
        prepare_staging(connection, args.block_table, args.horizon_table, epoch)
        done_blocks = existing_block_keys(connection, args.block_table)
        done_horizons = existing_horizon_keys(connection, args.horizon_table)
        generated_at = _generated_at(connection, args.block_table)
        groups = _group_units(units)
        inserted_blocks = 0
        inserted_horizons = 0
        for index, (scope, scope_units) in enumerate(sorted(groups.items(), key=lambda item: (item[0].view_kind, item[0].market_id, item[0].source)), start=1):
            pending = [unit for unit in scope_units if unit.key not in done_blocks]
            missing_horizon = not all((scope.market_id, scope.source, measure) in done_horizons for measure in _measures(scope.source))
            if not pending and not missing_horizon:
                continue
            native_rows = load_scope(connection, scope)
            blocks, horizons = build_scope_rows(scope, pending or scope_units[:1], native_rows, workers=args.workers, source_epoch=epoch, generated_at=generated_at)
            if not pending:
                blocks = []
            horizons = [row for row in horizons if (row.market_id, row.source, row.measure) not in done_horizons]
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
        print(json.dumps({"stage": "complete", "source_epoch": epoch, **counts, "stride_replay": replay, "contamination": contaminated, "generated_at": generated_at.isoformat(timespec="seconds")}), flush=True)
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
    return parser.parse_args()


def main() -> None:
    general_builder.apply_api_db_env_fallback()
    run(parse_args())


if __name__ == "__main__":
    main()
