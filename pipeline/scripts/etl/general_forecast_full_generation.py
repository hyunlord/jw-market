#!/usr/bin/env python3
"""Build general forecast/simulation caches safely in isolated staging tables."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import sys
from typing import Any, Final, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.scripts.etl import build_cache_deep_analysis_general as builder
from pipeline.scripts.etl.cache_build_common import mariadb_connect
from pipeline.scripts.etl.general_forecast_payload import (
    ContractGateError,
    optimize_and_mark_payload,
    optimize_validate_and_serialize,
    transform_cache_row,
    validate_payload_contract,
)


LIVE_MAIN_TABLE: Final[str] = builder.GENERAL_CACHE_TABLE
LIVE_HELPER_TABLE: Final[str] = builder.GENERAL_MARKET_FORECAST_TABLE

GroupKey = tuple[str, str]


@dataclass(frozen=True, slots=True)
class CompletionGateError(Exception):
    requested_count: int
    generated_count: int
    validated_count: int
    missing_generated: tuple[GroupKey, ...]
    missing_validated: tuple[GroupKey, ...]

    def __str__(self) -> str:
        return (
            f"completion_gate_failed requested={self.requested_count} "
            f"generated={self.generated_count} validated={self.validated_count} "
            f"missing_generated={list(self.missing_generated)[:10]} "
            f"missing_validated={list(self.missing_validated)[:10]}"
        )


@dataclass(frozen=True, slots=True)
class SwapPlan:
    forward: str
    reverse: str


@dataclass(frozen=True, slots=True)
class ResumeState:
    validated: set[GroupKey]
    pending: list[GroupKey]


class CheckpointStore:
    """Persist completed physical keys atomically for interruption-safe resume."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.completed_keys = self._load()

    def _load(self) -> set[GroupKey]:
        if not self.path.exists():
            return set()
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        return {(str(item[0]), str(item[1])) for item in payload.get("completed_keys", [])}

    def record_batch(self, keys: Iterable[GroupKey]) -> None:
        self.completed_keys.update(keys)
        payload = {
            "completed_keys": [list(key) for key in sorted(self.completed_keys)],
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def pending(self, requested: Iterable[GroupKey]) -> list[GroupKey]:
        return [key for key in requested if key not in self.completed_keys]


def load_worklist(path: Path) -> list[GroupKey]:
    """Load an exact physical-grain worklist from JSON or tab-separated text."""
    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        keys = [(str(item["brand_key"]), str(item["atc4_code"])) for item in payload]
    else:
        keys = []
        for index, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
            if not line.strip():
                continue
            parts = line.split("\t")
            if index == 0 and parts[:2] == ["brand_key", "atc4_code"]:
                continue
            if len(parts) < 2:
                raise SystemExit(f"invalid worklist line {index + 1}: {line!r}")
            keys.append((parts[0], parts[1]))
    if len(keys) != len(set(keys)):
        raise SystemExit("worklist contains duplicate physical keys")
    return keys


def resume_state(
    requested: list[GroupKey],
    *,
    staged_validated: set[GroupKey],
    checkpoint_keys: set[GroupKey],
) -> ResumeState:
    """Resume from durable validated rows; the local checkpoint is advisory."""
    requested_set = set(requested)
    validated = staged_validated & requested_set
    checkpoint_keys.intersection_update(validated)
    return ResumeState(
        validated=validated,
        pending=[key for key in requested if key not in validated],
    )


def assert_completion(
    requested: set[GroupKey],
    generated: set[GroupKey],
    validated: set[GroupKey],
) -> None:
    if requested == generated == validated:
        return
    raise CompletionGateError(
        requested_count=len(requested),
        generated_count=len(generated),
        validated_count=len(validated),
        missing_generated=tuple(sorted(requested - generated)),
        missing_validated=tuple(sorted(requested - validated)),
    )


def atomic_swap_plan(
    *,
    live_main: str,
    stage_main: str,
    backup_main: str,
    live_helper: str,
    stage_helper: str,
    backup_helper: str,
) -> SwapPlan:
    names = (live_main, stage_main, backup_main, live_helper, stage_helper, backup_helper)
    quoted = [builder.quote_ident(name) for name in names]
    lm, sm, bm, lh, sh, bh = quoted
    forward = f"RENAME TABLE {lm} TO {bm}, {sm} TO {lm}, {lh} TO {bh}, {sh} TO {lh}"
    reverse = f"RENAME TABLE {lm} TO {sm}, {bm} TO {lm}, {lh} TO {sh}, {bh} TO {lh}"
    return SwapPlan(forward=forward, reverse=reverse)


def execute_atomic_swap(conn: Any, plan: SwapPlan, *, confirmed: bool) -> None:
    """Execute one atomic multi-table rename after an explicit live-swap gate."""
    if not confirmed:
        raise SystemExit("live swap requires confirmed=True")
    with conn.cursor() as cur:
        cur.execute(plan.forward)


def _assert_staging_name(name: str, live_name: str) -> None:
    if name == live_name or not name.startswith(live_name + "_stage_"):
        raise SystemExit(f"staging table must start with {live_name}_stage_: {name}")


def ensure_staging_tables(conn: Any, *, main_table: str, helper_table: str) -> None:
    _assert_staging_name(main_table, LIVE_MAIN_TABLE)
    _assert_staging_name(helper_table, LIVE_HELPER_TABLE)
    main_exists = builder._table_exists(conn, main_table)
    helper_exists = builder._table_exists(conn, helper_table)
    with conn.cursor() as cur:
        if not main_exists:
            cur.execute(
                f"CREATE TABLE {builder.quote_ident(main_table)} "
                f"LIKE {builder.quote_ident(LIVE_MAIN_TABLE)}"
            )
        if not helper_exists:
            cur.execute(
                f"CREATE TABLE {builder.quote_ident(helper_table)} "
                f"LIKE {builder.quote_ident(LIVE_HELPER_TABLE)}"
            )
    conn.commit()


def _stage_keys(conn: Any, table_name: str) -> set[GroupKey]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT brand_key, atc4_code FROM {builder.quote_ident(table_name)}")
        return {(str(row["brand_key"]), str(row["atc4_code"])) for row in cur.fetchall()}


def _stage_validated_keys(conn: Any, table_name: str) -> set[GroupKey]:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT brand_key, atc4_code FROM {builder.quote_ident(table_name)} "
            "WHERE JSON_UNQUOTE(JSON_EXTRACT(response_json, '$.generation_status')) = 'generated'"
        )
        return {(str(row["brand_key"]), str(row["atc4_code"])) for row in cur.fetchall()}


def run_build(args: argparse.Namespace) -> None:
    _assert_staging_name(args.main_table, LIVE_MAIN_TABLE)
    _assert_staging_name(args.helper_table, LIVE_HELPER_TABLE)
    conn = mariadb_connect()
    checkpoint = CheckpointStore(Path(args.checkpoint))
    try:
        builder.assert_d2_database(conn)
        ensure_staging_tables(conn, main_table=args.main_table, helper_table=args.helper_table)
        requested_list = (
            load_worklist(Path(args.worklist))
            if args.worklist
            else builder.select_group_keys(
                conn,
                brands=set(args.brands) if args.brands else None,
                atc4=args.atc4,
                limit_groups=args.limit_groups,
            )
        )
        requested = set(requested_list)
        state = resume_state(
            requested_list,
            staged_validated=_stage_validated_keys(conn, args.main_table),
            checkpoint_keys=checkpoint.completed_keys,
        )
        validated = state.validated
        pending = state.pending
        for batch_index, group_batch in enumerate(builder.chunked(pending, args.group_batch_size), start=1):
            built = builder.build_batch_rows(
                conn,
                group_batch,
                workers=args.workers,
                verbose=args.verbose,
                market_table_name=args.helper_table,
                horizon_years=5,
            )
            transformed = [transform_cache_row(row) for row in built]
            built_keys = {(row.brand_key, row.atc4_code) for row in transformed}
            expected_batch = set(group_batch)
            assert_completion(expected_batch, built_keys, built_keys)
            builder.write_rows(conn, transformed, table_name=args.main_table, batch_size=args.batch_size)
            checkpoint.record_batch(built_keys)
            validated.update(built_keys)
            print(
                json.dumps(
                    {
                        "batch": batch_index,
                        "batch_rows": len(transformed),
                        "completed": len(checkpoint.completed_keys & requested),
                        "requested": len(requested),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        generated = _stage_keys(conn, args.main_table) & requested
        assert_completion(requested, generated, validated)
        print(
            json.dumps(
                {
                    "completion_gate": "pass",
                    "requested": len(requested),
                    "generated": len(generated),
                    "validated": len(validated),
                },
                ensure_ascii=False,
            )
        )
        if args.execute_swap:
            if not args.backup_main or not args.backup_helper:
                raise SystemExit("--execute-swap requires --backup-main and --backup-helper")
            plan = atomic_swap_plan(
                live_main=LIVE_MAIN_TABLE,
                stage_main=args.main_table,
                backup_main=args.backup_main,
                live_helper=LIVE_HELPER_TABLE,
                stage_helper=args.helper_table,
                backup_helper=args.backup_helper,
            )
            print(json.dumps({"forward_rename": plan.forward, "reverse_rename": plan.reverse}))
            execute_atomic_swap(conn, plan, confirmed=args.confirm_live_swap)
    finally:
        conn.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--main-table", required=True)
    parser.add_argument("--helper-table", required=True)
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--group-batch-size", type=int, default=100)
    parser.add_argument("--limit-groups", type=int)
    parser.add_argument("--brand", action="append", dest="brands")
    parser.add_argument("--atc4")
    parser.add_argument("--worklist")
    parser.add_argument("--execute-swap", action="store_true")
    parser.add_argument("--confirm-live-swap", action="store_true")
    parser.add_argument("--backup-main")
    parser.add_argument("--backup-helper")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> None:
    builder.apply_api_db_env_fallback()
    run_build(parse_args())


if __name__ == "__main__":
    main()
