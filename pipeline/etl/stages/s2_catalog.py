from __future__ import annotations

import argparse
import shutil
from pathlib import Path
from typing import Any

from pipeline.etl.io.catalog.dim.base_dimensions import run_base_dimensions
from pipeline.etl.io.catalog.brand.brand_product_catalog import run_brand_product_catalog
from pipeline.etl.io.catalog.postfix.catalog_postfix import run_postfix
from pipeline.etl.io.catalog.master.extracts import run_master_extracts
from pipeline.etl.io.catalog.market.catalog import run_market_catalog
from pipeline.etl.io.catalog.target.target_priority import run_target_priority

STAGE = "s2 catalog"


def _path_param(params: dict[str, Any], key: str) -> Path | None:
    value = params.get(key)
    return Path(str(value)) if value else None


def _str_param(params: dict[str, Any], key: str) -> str | None:
    value = params.get(key)
    return str(value) if value else None


def _copy_if_needed(source_dir: Path | None, output_root: Path, relative: str) -> None:
    if source_dir is None:
        return
    source = source_dir / Path(relative).name
    target = output_root / relative
    if target.exists():
        return
    if not source.exists():
        raise FileNotFoundError(f"required cache seed not found: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def run(params: dict[str, Any]) -> int:
    output_root = _path_param(params, "target_dir") or Path.cwd()
    input_file = _path_param(params, "input_file")
    catalog_path = _path_param(params, "catalog_path")
    cache_dir = _path_param(params, "cache_dir")
    inputs_dir = _path_param(params, "inputs_dir")
    ubist_dir = _path_param(params, "ubist_dir")
    iqvia_nsa_dir = _path_param(params, "iqvia_nsa_dir")
    ingested_at = _str_param(params, "ingested_at")
    try:
        _copy_if_needed(
            cache_dir,
            output_root,
            "data/cache/prototype_11_step_c4_target_priority_precompute_sample.csv",
        )
        master_results = run_master_extracts(
            output_root=output_root,
            input_file=input_file,
            catalog_path=catalog_path,
            ingested_at=ingested_at,
        )
        dimension_results = run_base_dimensions(
            output_root=output_root,
            ingested_at=ingested_at,
        )
        target_priority_results = run_target_priority(
            output_root=output_root,
            cache_dir=cache_dir,
            ubist_dir=ubist_dir,
            iqvia_nsa_dir=iqvia_nsa_dir,
            ingested_at=ingested_at,
        )
        market_catalog_results = run_market_catalog(
            output_root=output_root,
            ingested_at=ingested_at,
        )
        brand_product_catalog_results = run_brand_product_catalog(
            output_root=output_root,
            input_file=input_file,
            catalog_path=catalog_path,
            ubist_dir=ubist_dir,
            iqvia_nsa_dir=iqvia_nsa_dir,
            ingested_at=ingested_at,
        )
        postfix_results = run_postfix(output_root=output_root, inputs_dir=inputs_dir, ubist_dir=ubist_dir)
    except Exception as exc:
        print(f"[{STAGE}] catalog 생성 실패: {exc}")
        return 1

    for result in [
        *master_results,
        *dimension_results,
        *target_priority_results,
        *market_catalog_results,
        *brand_product_catalog_results,
        *postfix_results,
    ]:
        print(
            f"[{STAGE}] {result.name}: rows={result.rows} "
            f"columns={len(result.columns)} path={result.output_path}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", type=Path, default=Path.cwd())
    parser.add_argument("--input-file", type=Path, default=None)
    parser.add_argument("--catalog-path", type=Path, default=None)
    parser.add_argument("--cache-dir", type=Path, default=None)
    parser.add_argument("--inputs-dir", type=Path, default=None)
    parser.add_argument("--ubist-dir", type=Path, default=None)
    parser.add_argument("--iqvia-nsa-dir", type=Path, default=None)
    parser.add_argument("--ingested-at", default=None)
    args = parser.parse_args(argv)
    return run(
        {
            "target_dir": args.output_root,
            "input_file": args.input_file,
            "catalog_path": args.catalog_path,
            "cache_dir": args.cache_dir,
            "inputs_dir": args.inputs_dir,
            "ubist_dir": args.ubist_dir,
            "iqvia_nsa_dir": args.iqvia_nsa_dir,
            "ingested_at": args.ingested_at,
        }
    )


if __name__ == "__main__":
    raise SystemExit(main())
