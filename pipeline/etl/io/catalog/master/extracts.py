from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pyarrow.parquet as pq

from pipeline.etl.io.catalog.master import brand_consolidation as master_brand_consolidation
from pipeline.etl.io.catalog.master import drug as master_drug
from pipeline.etl.io.catalog.master import mapping_table as master_mapping_table
from pipeline.etl.io.catalog.master import market_definition as master_market_definition
from pipeline.etl.io.catalog.master import qa as master_qa
from pipeline.etl.lib.storage import get_mi_master_path

PROJECT_ROOT = Path(__file__).resolve().parents[5]
DEFAULT_MAPPING_CATALOG = PROJECT_ROOT / "pipeline" / "etl" / "config" / "master_column_mapping_catalog.md"


@dataclass(frozen=True)
class MasterExtractResult:
    name: str
    output_path: Path
    rows: int
    columns: tuple[str, ...]


def _output(root: Path, relative: str) -> Path:
    return Path(root) / relative


def _result(name: str, output_path: Path) -> MasterExtractResult:
    table = pq.read_table(output_path)
    return MasterExtractResult(
        name=name,
        output_path=output_path,
        rows=table.num_rows,
        columns=tuple(table.schema.names),
    )


def run_master_market_definition(
    *,
    output_root: Path = PROJECT_ROOT,
    input_file: Path | None = None,
    ingested_at: str | None = None,
) -> MasterExtractResult:
    xlsx_path = Path(input_file or get_mi_master_path())
    output_path = _output(output_root, "parquet/master_market_definition/master_market_definition.parquet")
    records = list(master_market_definition.iter_market_definition_rows(xlsx_path, ingested_at=ingested_at))
    master_market_definition.validate_records(records)
    master_market_definition.write_parquet(records, output_path)
    return _result("master_market_definition", output_path)


def run_master_qa(
    *,
    output_root: Path = PROJECT_ROOT,
    input_file: Path | None = None,
    ingested_at: str | None = None,
) -> MasterExtractResult:
    xlsx_path = Path(input_file or get_mi_master_path())
    output_path = _output(output_root, "parquet/master_qa/master_qa.parquet")
    records, _stats = master_qa.load_qa_records(xlsx_path, ingested_at=ingested_at)
    master_qa.validate_records(records)
    master_qa.write_parquet(records, output_path)
    return _result("master_qa", output_path)


def run_master_brand_consolidation(
    *,
    output_root: Path = PROJECT_ROOT,
    input_file: Path | None = None,
    ingested_at: str | None = None,
) -> MasterExtractResult:
    xlsx_path = Path(input_file or get_mi_master_path())
    output_path = _output(output_root, "parquet/master_brand_consolidation/master_brand_consolidation.parquet")
    records, stats = master_brand_consolidation.load_brand_consolidation_records(
        xlsx_path,
        ingested_at=ingested_at,
    )
    master_brand_consolidation.validate_records(records, stats)
    master_brand_consolidation.write_parquet(records, output_path)
    return _result("master_brand_consolidation", output_path)


def run_master_mapping_table(
    *,
    output_root: Path = PROJECT_ROOT,
    input_file: Path | None = None,
    catalog_path: Path | None = None,
    ingested_at: str | None = None,
) -> MasterExtractResult:
    xlsx_path = master_mapping_table.resolve_input_file(Path(input_file or get_mi_master_path()))
    output_path = _output(output_root, "parquet/master_mapping_table/master_mapping_table.parquet")
    records, stats = master_mapping_table.load_mapping_records(
        xlsx_path,
        Path(catalog_path or DEFAULT_MAPPING_CATALOG),
        ingested_at=ingested_at,
    )
    master_mapping_table.validate_records(records, stats)
    master_mapping_table.write_parquet(records, output_path)
    return _result("master_mapping_table", output_path)


def run_master_drug(
    *,
    output_root: Path = PROJECT_ROOT,
    input_file: Path | None = None,
    catalog_path: Path | None = None,
    ingested_at: str | None = None,
) -> MasterExtractResult:
    xlsx_path = master_drug.resolve_input_file(Path(input_file or get_mi_master_path()))
    output_path = _output(output_root, "parquet/master_drug/master_drug.parquet")
    mapping = Path(catalog_path or DEFAULT_MAPPING_CATALOG)
    records, stats = master_drug.load_drug_records(xlsx_path, mapping, ingested_at=ingested_at)
    master_drug.validate_records(records, stats, mapping)
    master_drug.write_parquet(records, output_path)
    return _result("master_drug", output_path)


MASTER_EXTRACTS: tuple[Callable[..., MasterExtractResult], ...] = (
    run_master_market_definition,
    run_master_qa,
    run_master_brand_consolidation,
    run_master_mapping_table,
    run_master_drug,
)


def run_master_extracts(
    *,
    output_root: Path = PROJECT_ROOT,
    input_file: Path | None = None,
    catalog_path: Path | None = None,
    ingested_at: str | None = None,
) -> list[MasterExtractResult]:
    output_root = Path(output_root)
    input_path = Path(input_file or get_mi_master_path())
    mapping = Path(catalog_path or DEFAULT_MAPPING_CATALOG)
    return [
        run_master_market_definition(output_root=output_root, input_file=input_path, ingested_at=ingested_at),
        run_master_qa(output_root=output_root, input_file=input_path, ingested_at=ingested_at),
        run_master_brand_consolidation(output_root=output_root, input_file=input_path, ingested_at=ingested_at),
        run_master_mapping_table(
            output_root=output_root,
            input_file=input_path,
            catalog_path=mapping,
            ingested_at=ingested_at,
        ),
        run_master_drug(
            output_root=output_root,
            input_file=input_path,
            catalog_path=mapping,
            ingested_at=ingested_at,
        ),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run s2-a master extracts.")
    parser.add_argument("--input-file", type=Path, default=None)
    parser.add_argument("--catalog-path", type=Path, default=DEFAULT_MAPPING_CATALOG)
    parser.add_argument("--output-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--ingested-at", default=None)
    args = parser.parse_args(argv)
    results = run_master_extracts(
        output_root=args.output_root,
        input_file=args.input_file,
        catalog_path=args.catalog_path,
        ingested_at=args.ingested_at,
    )
    for result in results:
        print(f"{result.name}	rows={result.rows}	columns={len(result.columns)}	path={result.output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
