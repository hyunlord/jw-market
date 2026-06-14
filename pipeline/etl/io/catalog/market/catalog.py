from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

import pyarrow.parquet as pq

from pipeline.etl.io.catalog.market import cd_filter as cd_filter
from pipeline.etl.io.catalog.market import cd_market as cd_market
from pipeline.etl.io.catalog.market import ml_market as ml_market

PROJECT_ROOT = Path(__file__).resolve().parents[5]


@dataclass(frozen=True)
class MarketCatalogResult:
    name: str
    output_path: Path
    rows: int
    columns: tuple[str, ...]


def _output(root: Path, relative: str) -> Path:
    return Path(root) / relative


def _result(name: str, output_path: Path) -> MarketCatalogResult:
    table = pq.read_table(output_path)
    return MarketCatalogResult(
        name=name,
        output_path=output_path,
        rows=table.num_rows,
        columns=tuple(table.schema.names),
    )


def _ingested_at(value):
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def run_ml_market(
    *,
    output_root: Path = PROJECT_ROOT,
    ingested_at=None,
) -> MarketCatalogResult:
    market_definition = _output(output_root, "parquet/master_market_definition/master_market_definition.parquet")
    master_drug = _output(output_root, "parquet/master_drug/master_drug.parquet")
    output_path = _output(output_root, "parquet/ml_market/ml_market.parquet")
    records = ml_market.load_ml_market_records(
        market_definition,
        master_drug,
        output_path,
        ingested_at=_ingested_at(ingested_at),
    )
    ml_market.write_parquet(records, output_path)
    ml_market.validate_written_parquet(output_path)
    return _result("ml_market", output_path)


def run_cd_filter(
    *,
    output_root: Path = PROJECT_ROOT,
    ingested_at=None,
) -> MarketCatalogResult:
    market_definition = _output(output_root, "parquet/master_market_definition/master_market_definition.parquet")
    output_path = _output(output_root, "parquet/cd_filter/cd_filter.parquet")
    records = cd_filter.load_cd_filter_records(market_definition, ingested_at=_ingested_at(ingested_at))
    cd_filter.write_parquet(records, output_path)
    cd_filter.validate_written_parquet(output_path)
    return _result("cd_filter", output_path)


def run_cd_market(
    *,
    output_root: Path = PROJECT_ROOT,
    ingested_at=None,
) -> MarketCatalogResult:
    ml_market_path = _output(output_root, "parquet/ml_market/ml_market.parquet")
    cd_filter_path = _output(output_root, "parquet/cd_filter/cd_filter.parquet")
    market_definition = _output(output_root, "parquet/master_market_definition/master_market_definition.parquet")
    output_path = _output(output_root, "parquet/cd_market/cd_market.parquet")
    records = cd_market.load_cd_market_records(
        ml_market_path,
        cd_filter_path,
        market_definition,
        output_path,
        ingested_at=_ingested_at(ingested_at),
    )
    cd_market.write_parquet(records, output_path)
    return _result("cd_market", output_path)


MARKET_CATALOG_STEPS: tuple[Callable[..., MarketCatalogResult], ...] = (
    run_ml_market,
    run_cd_filter,
    run_cd_market,
)


def run_market_catalog(
    *,
    output_root: Path = PROJECT_ROOT,
    ingested_at=None,
) -> list[MarketCatalogResult]:
    return [step(output_root=output_root, ingested_at=ingested_at) for step in MARKET_CATALOG_STEPS]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run s2-d1 market catalog extracts.")
    parser.add_argument("--output-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--ingested-at", default=None)
    args = parser.parse_args(argv)
    results = run_market_catalog(output_root=args.output_root, ingested_at=args.ingested_at)
    for result in results:
        print(f"{result.name}\trows={result.rows}\tcolumns={len(result.columns)}\tpath={result.output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
