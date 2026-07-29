"""Materialize a checksummed S2 catalog snapshot for runtime consumers."""

from __future__ import annotations

import argparse
from contextlib import nullcontext
from pathlib import Path
import tempfile

from pipeline.etl.io.catalog.paths import (
    S2_REQUIRED_CATALOGS,
    materialize_catalog,
)
from pipeline.etl.lib.storage import sync_minio_to_local


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Materialize and verify output/catalog from immutable storage."
    )
    parser.add_argument("--backend", choices=("local", "minio"), default="local")
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--destination-root", type=Path, required=True)
    parser.add_argument("--bucket")
    parser.add_argument("--prefix", default="")
    parser.add_argument("--required-name", action="append", default=[])
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    required = frozenset(args.required_name) or S2_REQUIRED_CATALOGS
    if args.backend == "local":
        if args.source_root is None:
            raise SystemExit("--source-root is required for --backend local")
        source_context = nullcontext(args.source_root)
    else:
        if not args.bucket:
            raise SystemExit("--bucket is required for --backend minio")
        source_context = tempfile.TemporaryDirectory(prefix="jw-market-catalog-")

    with source_context as source_value:
        source_root = Path(source_value)
        if args.backend == "minio":
            sync_minio_to_local(
                args.bucket,
                args.prefix,
                source_root,
                overwrite=False,
                progress=True,
            )
        materialized = materialize_catalog(
            source_root=source_root,
            destination_root=args.destination_root,
            required_names=required,
        )
    print(
        f"catalog_materialized root={args.destination_root.resolve()} "
        f"artifacts={len(materialized)} required={','.join(sorted(required))}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
