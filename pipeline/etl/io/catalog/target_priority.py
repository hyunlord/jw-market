from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

import pyarrow.parquet as pq

from pipeline.etl.io.catalog import dim_market_target_priority

PROJECT_ROOT = Path(__file__).resolve().parents[4]


@dataclass(frozen=True)
class TargetPriorityResult:
    name: str
    rows: int
    columns: tuple[str, ...]
    output_path: Path
    cache_path: Path


def _output(root: Path, relative: str) -> Path:
    return root / relative


def _result(name: str, output_path: Path, cache_path: Path) -> TargetPriorityResult:
    table = pq.read_table(output_path)
    return TargetPriorityResult(
        name=name,
        rows=table.num_rows,
        columns=tuple(table.column_names),
        output_path=output_path,
        cache_path=cache_path,
    )


def run_target_priority(
    *,
    output_root: Path = PROJECT_ROOT,
    ingested_at: str | None = None,
) -> list[TargetPriorityResult]:
    output_path = _output(
        output_root,
        "parquet/dim_market_target_priority/dim_market_target_priority.parquet",
    )
    cache_path = _output(
        output_root,
        "data/cache/prototype_12_round6_auto_fill_customer_dictionary_estimate.csv",
    )
    ubist_path = dim_market_target_priority.resolve_ubist_latest(output_root / "output/ubist")
    iqvia_path = dim_market_target_priority.resolve_iqvia_latest(output_root / "output/iqvia_nsa")
    records = dim_market_target_priority.load_dim_market_target_priority_records(
        skeleton_path=output_root
        / "data/cache/prototype_11_step_c4_target_priority_precompute_sample.csv",
        dim_competitive_path=output_root
        / "parquet/dim_market_competitive_dynamics/dim_market_competitive_dynamics.parquet",
        master_drug_path=output_root / "parquet/master_drug/master_drug.parquet",
        ubist_path=ubist_path,
        iqvia_path=iqvia_path,
        cache_path=cache_path,
        ingested_at=ingested_at,
    )
    dim_market_target_priority.write_parquet(records, output_path)
    return [_result("dim_market_target_priority", output_path, cache_path)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--ingested-at", default=None)
    args = parser.parse_args(argv)
    results = run_target_priority(output_root=args.output_root, ingested_at=args.ingested_at)
    for result in results:
        print(
            f"{result.name}\trows={result.rows}\tcolumns={len(result.columns)}"
            f"\tpath={result.output_path}\tcache={result.cache_path}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
