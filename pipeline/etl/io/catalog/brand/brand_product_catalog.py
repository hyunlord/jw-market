from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

import pyarrow.parquet as pq

from pipeline.etl.io.catalog.brand import cd_brand as cd_brand
from pipeline.etl.io.catalog.brand import cd_product as cd_product
from pipeline.etl.io.catalog.brand import strategic_brand as strategic_brand
from pipeline.etl.io.catalog.brand import strategic_product as strategic_product

PROJECT_ROOT = Path(__file__).resolve().parents[5]


@dataclass(frozen=True)
class BrandProductCatalogResult:
    name: str
    output_path: Path
    rows: int
    columns: tuple[str, ...]


def _output(root: Path, relative: str) -> Path:
    return Path(root) / relative


def _result(name: str, output_path: Path) -> BrandProductCatalogResult:
    table = pq.read_table(output_path)
    return BrandProductCatalogResult(
        name=name,
        output_path=output_path,
        rows=table.num_rows,
        columns=tuple(table.schema.names),
    )


def _ingested_at(value: str | datetime | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def run_strategic_brand(
    *,
    output_root: Path = PROJECT_ROOT,
    input_file: Path | None = None,
    catalog_path: Path | None = None,
    ubist_dir: Path | None = None,
    iqvia_nsa_dir: Path | None = None,
    ingested_at: str | datetime | None = None,
) -> BrandProductCatalogResult:
    ml_market = _output(output_root, "parquet/ml_market/ml_market.parquet")
    cd_filter = _output(output_root, "parquet/cd_filter/cd_filter.parquet")
    cd_market = _output(output_root, "parquet/cd_market/cd_market.parquet")
    output_path = _output(output_root, "parquet/strategic_brand/strategic_brand.parquet")
    cache_path = _output(output_root, "data/cache/prototype_14_step5_gadrelet_brand_mapping.csv")
    records, summary = strategic_brand.load_strategic_brand_records(
        ml_market,
        cd_filter,
        cd_market,
        input_file=input_file,
        catalog_path=catalog_path,
        ubist_dir=ubist_dir or _output(output_root, "output/ubist"),
        iqvia_nsa_dir=iqvia_nsa_dir or _output(output_root, "output/iqvia_nsa"),
        ingested_at=_ingested_at(ingested_at),
    )
    strategic_brand.write_parquet(records, output_path)
    strategic_brand.validate_written_parquet(output_path)
    strategic_brand.write_gadrelet_cache(summary["gadrelet_rows"], cache_path)
    return _result("strategic_brand", output_path)


def run_strategic_product(
    *,
    output_root: Path = PROJECT_ROOT,
    input_file: Path | None = None,
    catalog_path: Path | None = None,
    ubist_dir: Path | None = None,
    iqvia_nsa_dir: Path | None = None,
    ingested_at: str | datetime | None = None,
) -> BrandProductCatalogResult:
    strategic_brand_path = _output(output_root, "parquet/strategic_brand/strategic_brand.parquet")
    ml_market = _output(output_root, "parquet/ml_market/ml_market.parquet")
    cd_market = _output(output_root, "parquet/cd_market/cd_market.parquet")
    ubist_path = strategic_product.resolve_ubist_latest(ubist_dir or _output(output_root, "output/ubist"))
    iqvia_path = strategic_product.resolve_iqvia_latest(iqvia_nsa_dir or _output(output_root, "output/iqvia_nsa"))
    output_path = _output(output_root, "parquet/strategic_product/strategic_product.parquet")
    cache_path = _output(output_root, "data/cache/prototype_14_step6_product_match_coverage.csv")
    records, coverage_rows = strategic_product.load_strategic_product_records(
        strategic_brand_path,
        ml_market,
        cd_market,
        ubist_path,
        iqvia_path,
        input_file=input_file,
        catalog_path=catalog_path,
        ingested_at=_ingested_at(ingested_at),
    )
    strategic_product.write_parquet(records, output_path)
    strategic_product.validate_written_parquet(output_path)
    strategic_product.write_coverage_cache(coverage_rows, cache_path)
    return _result("strategic_product", output_path)


def run_cd_brand(
    *,
    output_root: Path = PROJECT_ROOT,
    input_file: Path | None = None,
    catalog_path: Path | None = None,
    ingested_at: str | datetime | None = None,
) -> BrandProductCatalogResult:
    strategic_brand_path = _output(output_root, "parquet/strategic_brand/strategic_brand.parquet")
    cd_market = _output(output_root, "parquet/cd_market/cd_market.parquet")
    cd_filter = _output(output_root, "parquet/cd_filter/cd_filter.parquet")
    output_path = _output(output_root, "parquet/cd_brand/cd_brand.parquet")
    records, _mismatches = cd_brand.load_cd_brand_records(
        strategic_brand_path,
        cd_market,
        cd_filter,
        input_file=input_file,
        catalog_path=catalog_path,
        ingested_at=_ingested_at(ingested_at),
    )
    cd_brand.write_parquet(records, output_path)
    cd_brand.validate_written_parquet(output_path)
    return _result("cd_brand", output_path)


def run_cd_product(
    *,
    output_root: Path = PROJECT_ROOT,
    ingested_at: str | datetime | None = None,
) -> BrandProductCatalogResult:
    strategic_product_path = _output(output_root, "parquet/strategic_product/strategic_product.parquet")
    cd_brand_path = _output(output_root, "parquet/cd_brand/cd_brand.parquet")
    cd_market = _output(output_root, "parquet/cd_market/cd_market.parquet")
    output_path = _output(output_root, "parquet/cd_product/cd_product.parquet")
    records = cd_product.load_cd_product_records(
        strategic_product_path,
        cd_brand_path,
        cd_market,
        ingested_at=_ingested_at(ingested_at),
    )
    cd_product.write_parquet(records, output_path)
    cd_product.validate_written_parquet(output_path)
    return _result("cd_product", output_path)


BRAND_PRODUCT_CATALOG_STEPS: tuple[Callable[..., BrandProductCatalogResult], ...] = (
    run_strategic_brand,
    run_strategic_product,
    run_cd_brand,
    run_cd_product,
)


def run_brand_product_catalog(
    *,
    output_root: Path = PROJECT_ROOT,
    input_file: Path | None = None,
    catalog_path: Path | None = None,
    ubist_dir: Path | None = None,
    iqvia_nsa_dir: Path | None = None,
    ingested_at: str | datetime | None = None,
) -> list[BrandProductCatalogResult]:
    return [
        run_strategic_brand(
            output_root=output_root,
            input_file=input_file,
            catalog_path=catalog_path,
            ubist_dir=ubist_dir,
            iqvia_nsa_dir=iqvia_nsa_dir,
            ingested_at=ingested_at,
        ),
        run_strategic_product(
            output_root=output_root,
            input_file=input_file,
            catalog_path=catalog_path,
            ubist_dir=ubist_dir,
            iqvia_nsa_dir=iqvia_nsa_dir,
            ingested_at=ingested_at,
        ),
        run_cd_brand(
            output_root=output_root,
            input_file=input_file,
            catalog_path=catalog_path,
            ingested_at=ingested_at,
        ),
        run_cd_product(output_root=output_root, ingested_at=ingested_at),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run s2-d2 brand/product catalog extracts.")
    parser.add_argument("--output-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--ingested-at", default=None)
    args = parser.parse_args(argv)
    results = run_brand_product_catalog(output_root=args.output_root, ingested_at=args.ingested_at)
    for result in results:
        print(f"{result.name}\trows={result.rows}\tcolumns={len(result.columns)}\tpath={result.output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
