from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from pipeline.etl.io.catalog.base_dimensions import run_base_dimensions
from pipeline.etl.io.catalog.master_extracts import run_master_extracts
from pipeline.etl.io.catalog.target_priority import run_target_priority

STAGE = "s2 catalog"


def _path_param(params: dict[str, Any], key: str) -> Path | None:
    value = params.get(key)
    return Path(str(value)) if value else None


def _str_param(params: dict[str, Any], key: str) -> str | None:
    value = params.get(key)
    return str(value) if value else None


def run(params: dict[str, Any]) -> int:
    output_root = _path_param(params, "target_dir") or Path.cwd()
    input_file = _path_param(params, "input_file")
    catalog_path = _path_param(params, "catalog_path")
    ingested_at = _str_param(params, "ingested_at")
    try:
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
            ingested_at=ingested_at,
        )
    except Exception as exc:
        print(f"[{STAGE}] catalog 생성 실패: {exc}")
        return 1

    for result in [*master_results, *dimension_results, *target_priority_results]:
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
    parser.add_argument("--ingested-at", default=None)
    args = parser.parse_args(argv)
    return run(
        {
            "target_dir": args.output_root,
            "input_file": args.input_file,
            "catalog_path": args.catalog_path,
            "ingested_at": args.ingested_at,
        }
    )


if __name__ == "__main__":
    raise SystemExit(main())
