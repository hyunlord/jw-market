#!/usr/bin/env python3
"""Update only target_customer_competition in the three market marts.

Phase 16-G-4-Fix-PreCache.

The brand marts do not persist ``channel_specialty_matrix``. For UBIST, this
script therefore reuses the verified Phase 16-G-4-Fix-Load dry-run JSONL rows
as the calculation source, then updates only the market mart
``target_customer_competition`` column.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd

from layer3_compute_general_v3 import ALLOWED_SOURCES, mariadb_connect, read_jsonl
from layer3_compute_market_metric import compute_target_competition_iqvia, compute_target_competition_ubist
from ops_utils import configure_logging, find_project_root


LOGGER = configure_logging(__name__)
PROJECT_ROOT = find_project_root(Path(__file__).resolve())
CATALOG_DIR = PROJECT_ROOT / "output" / "catalog"
DEFAULT_DRY_RUN_DIR = Path("/tmp/dryrun_fix_load_full")


def clean_row(row: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in row.items():
        is_missing = False
        if not isinstance(value, (list, dict, tuple)):
            try:
                is_missing = bool(pd.isna(value))
            except (TypeError, ValueError):
                is_missing = False
        if is_missing:
            cleaned[key] = None
        else:
            cleaned[key] = value
    return cleaned


def catalog_maps() -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    ml = pd.read_parquet(CATALOG_DIR / "ml_market" / "ml_market.parquet")
    cd = pd.read_parquet(CATALOG_DIR / "cd_market" / "cd_market.parquet")
    ml_map = {str(row["ml_id"]): clean_row(row.to_dict()) for _, row in ml.iterrows()}
    cd_map = {str(row["cd_id"]): clean_row(row.to_dict()) for _, row in cd.iterrows()}
    return ml_map, cd_map


def unique_rows(rows: Iterable[dict[str, Any]], key_fields: tuple[str, ...]) -> list[dict[str, Any]]:
    by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        by_key[tuple(row.get(field) for field in key_fields)] = row
    return list(by_key.values())


def group_rows(rows: Iterable[dict[str, Any]], key_fields: tuple[str, ...]) -> dict[tuple[Any, ...], list[dict[str, Any]]]:
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[tuple(row.get(field) for field in key_fields)].append(row)
    return grouped


def load_general_rows(dry_run_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for source in ALLOWED_SOURCES:
        rows.extend(read_jsonl(dry_run_dir / f"general_v3_{source}_brand_rows.jsonl"))
    return unique_rows(rows, ("brand_key", "atc4_code", "source", "measure"))


def load_ml_rows(dry_run_dir: Path) -> list[dict[str, Any]]:
    return unique_rows(
        read_jsonl(dry_run_dir / "strategic_ml_v3_brand_rows.jsonl"),
        ("ml_id", "brand_id", "source", "measure"),
    )


def load_cd_rows(dry_run_dir: Path) -> list[dict[str, Any]]:
    return unique_rows(
        read_jsonl(dry_run_dir / "strategic_cd_v3_brand_rows.jsonl"),
        ("cd_market_id", "cd_brand_id", "source", "measure"),
    )


def target_for(rows: list[dict[str, Any]], source: str, catalog_row: dict[str, Any] | None) -> dict[str, Any]:
    if source == "ubist":
        return compute_target_competition_ubist(rows, catalog_row)
    if source == "iqvia_nsa":
        return compute_target_competition_iqvia(rows, catalog_row)
    raise ValueError(f"unsupported source: {source}")


def update_market_table(
    table: str,
    market_col: str,
    grouped: dict[tuple[Any, str, str], list[dict[str, Any]]],
    catalog_by_market: dict[str, dict[str, Any]] | None,
    dry_run: bool,
) -> int:
    updates = []
    for (market_id, source, measure), rows in sorted(grouped.items()):
        catalog_row = catalog_by_market.get(str(market_id)) if catalog_by_market else None
        target = target_for(rows, str(source), catalog_row)
        updates.append((json.dumps(target, ensure_ascii=False), market_id, source, measure))

    if dry_run:
        LOGGER.info("[dry-run] %s target updates: %s", table, len(updates))
        return len(updates)

    conn = mariadb_connect()
    try:
        with conn.cursor() as cur:
            cur.executemany(
                f"""
                UPDATE {table}
                SET target_customer_competition = %s
                WHERE {market_col} = %s AND source = %s AND measure = %s
                """,
                updates,
            )
        conn.commit()
    finally:
        conn.close()
    LOGGER.info("%s target updates applied: %s", table, len(updates))
    return len(updates)


def run(dry_run_dir: Path, dry_run: bool) -> dict[str, int]:
    ml_map, cd_map = catalog_maps()
    general_rows = load_general_rows(dry_run_dir)
    ml_rows = load_ml_rows(dry_run_dir)
    cd_rows = load_cd_rows(dry_run_dir)

    counts = {
        "mart_general_market_metric": update_market_table(
            "mart_general_market_metric",
            "atc4_code",
            group_rows(general_rows, ("atc4_code", "source", "measure")),
            None,
            dry_run,
        ),
        "mart_strategic_ml_market_metric": update_market_table(
            "mart_strategic_ml_market_metric",
            "ml_id",
            group_rows(ml_rows, ("ml_id", "source", "measure")),
            ml_map,
            dry_run,
        ),
        "mart_strategic_cd_market_metric": update_market_table(
            "mart_strategic_cd_market_metric",
            "cd_market_id",
            group_rows(cd_rows, ("cd_market_id", "source", "measure")),
            cd_map,
            dry_run,
        ),
    }
    return counts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run-dir", type=Path, default=DEFAULT_DRY_RUN_DIR)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    counts = run(args.dry_run_dir, args.dry_run)
    print(json.dumps(counts, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
