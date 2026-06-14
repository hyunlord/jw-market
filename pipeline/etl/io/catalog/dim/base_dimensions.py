from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pyarrow.parquet as pq

from pipeline.etl.io.catalog.dim import brand_group as dim_brand_group
from pipeline.etl.io.catalog.dim import jw_products as dim_jw_products
from pipeline.etl.io.catalog.dim import market_competitive_dynamics as dim_market_competitive_dynamics
from pipeline.etl.io.catalog.dim import market_landscape as dim_market_landscape

PROJECT_ROOT = Path(__file__).resolve().parents[5]


@dataclass(frozen=True)
class BaseDimensionResult:
    name: str
    output_path: Path
    rows: int
    columns: tuple[str, ...]


def _output(root: Path, relative: str) -> Path:
    return Path(root) / relative


def _result(name: str, output_path: Path) -> BaseDimensionResult:
    table = pq.read_table(output_path)
    return BaseDimensionResult(
        name=name,
        output_path=output_path,
        rows=table.num_rows,
        columns=tuple(table.schema.names),
    )


def run_dim_jw_products(
    *,
    output_root: Path = PROJECT_ROOT,
    ingested_at: str | None = None,
) -> BaseDimensionResult:
    market_definition = _output(output_root, "parquet/master_market_definition/master_market_definition.parquet")
    master_qa = _output(output_root, "parquet/master_qa/master_qa.parquet")
    output_path = _output(output_root, "parquet/dim_jw_products/dim_jw_products.parquet")
    records = dim_jw_products.load_dim_jw_product_records(
        market_definition,
        master_qa,
        ingested_at=ingested_at,
    )
    dim_jw_products.write_parquet(records, output_path)
    return _result("dim_jw_products", output_path)


def run_dim_brand_group(
    *,
    output_root: Path = PROJECT_ROOT,
    ingested_at: str | None = None,
) -> list[BaseDimensionResult]:
    brand_consolidation = _output(
        output_root,
        "parquet/master_brand_consolidation/master_brand_consolidation.parquet",
    )
    master_drug = _output(output_root, "parquet/master_drug/master_drug.parquet")
    group_path = _output(output_root, "parquet/dim_brand_group/dim_brand_group.parquet")
    members_path = _output(
        output_root,
        "parquet/master_brand_consolidation_members/master_brand_consolidation_members.parquet",
    )
    group_records, member_records = dim_brand_group.load_brand_group_outputs(
        brand_consolidation,
        master_drug,
        ingested_at=ingested_at,
    )
    dim_brand_group.write_dim_brand_group(group_records, group_path)
    dim_brand_group.write_members(member_records, members_path)
    return [
        _result("dim_brand_group", group_path),
        _result("master_brand_consolidation_members", members_path),
    ]


def run_dim_market_landscape(
    *,
    output_root: Path = PROJECT_ROOT,
    ingested_at: str | None = None,
) -> BaseDimensionResult:
    market_definition = _output(output_root, "parquet/master_market_definition/master_market_definition.parquet")
    master_drug = _output(output_root, "parquet/master_drug/master_drug.parquet")
    output_path = _output(output_root, "parquet/dim_market_landscape/dim_market_landscape.parquet")
    records = dim_market_landscape.load_dim_market_landscape_records(
        market_definition,
        master_drug,
        ingested_at=ingested_at,
    )
    dim_market_landscape.write_parquet(records, output_path)
    return _result("dim_market_landscape", output_path)


def run_dim_market_competitive_dynamics(
    *,
    output_root: Path = PROJECT_ROOT,
    ingested_at: str | None = None,
) -> BaseDimensionResult:
    dim_landscape = _output(output_root, "parquet/dim_market_landscape/dim_market_landscape.parquet")
    market_definition = _output(output_root, "parquet/master_market_definition/master_market_definition.parquet")
    master_drug = _output(output_root, "parquet/master_drug/master_drug.parquet")
    output_path = _output(
        output_root,
        "parquet/dim_market_competitive_dynamics/dim_market_competitive_dynamics.parquet",
    )
    records = dim_market_competitive_dynamics.load_dim_market_competitive_dynamics_records(
        dim_landscape,
        market_definition,
        master_drug,
        ingested_at=ingested_at,
    )
    dim_market_competitive_dynamics.write_parquet(records, output_path)
    return _result("dim_market_competitive_dynamics", output_path)


BASE_DIMENSION_STEPS: tuple[Callable[..., BaseDimensionResult | list[BaseDimensionResult]], ...] = (
    run_dim_jw_products,
    run_dim_brand_group,
    run_dim_market_landscape,
    run_dim_market_competitive_dynamics,
)


def run_base_dimensions(
    *,
    output_root: Path = PROJECT_ROOT,
    ingested_at: str | None = None,
) -> list[BaseDimensionResult]:
    results: list[BaseDimensionResult] = []
    for step in BASE_DIMENSION_STEPS:
        result = step(output_root=output_root, ingested_at=ingested_at)
        if isinstance(result, list):
            results.extend(result)
        else:
            results.append(result)
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run s2-b base dimension extracts.")
    parser.add_argument("--output-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--ingested-at", default=None)
    args = parser.parse_args(argv)
    results = run_base_dimensions(output_root=args.output_root, ingested_at=args.ingested_at)
    for result in results:
        print(f"{result.name}	rows={result.rows}	columns={len(result.columns)}	path={result.output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
