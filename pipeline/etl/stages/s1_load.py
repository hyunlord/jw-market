"""Stage s1 load - raw source dispatcher."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from pipeline.etl.io import iqvia_loader
from pipeline.etl.io.iqvia_roles import bind_iqvia_sources, canonical_nsa_source
from pipeline.etl.io.ubist_loader import (
    TARGET_DIR,
    discover_xlsx,
    dry_run,
    run_incremental_ubist_load,
    run_ubist_load,
)

STAGE = "s1 load"
VALID_SOURCES = {"ubist", "iqvia", "all"}


def _run_ubist(params: dict[str, Any]) -> int:
    target = Path(params["target_dir"]) if params.get("target_dir") else TARGET_DIR
    mode = params.get("ubist_mode") or "replace"
    dry = bool(params.get("dry_run"))
    incremental = bool(params.get("incremental"))
    file_arg = params.get("file")
    source_files = tuple(
        Path(str(value)).resolve() for value in params.get("source_files") or ()
    )
    source_dir = Path(str(params["ubist_source_dir"])) if params.get("ubist_source_dir") else None
    exclude_periods = frozenset(params.get("exclude_ubist_months") or ())
    try:
        if incremental:
            stats = run_incremental_ubist_load(
                target=target,
                file=Path(str(file_arg)) if file_arg else None,
                all_sources=not bool(file_arg),
                dry=dry,
            )
            if dry:
                print(f"[{STAGE}] UBIST incremental dry-run 완료 target={target}")
                return 0
            total_rows = sum(stat.row_count for stat in stats.values())
            print(
                f"[{STAGE}] UBIST incremental load 완료 "
                f"target={target} partitions={len(stats)} rows={total_rows}"
            )
            for period in sorted(stats):
                print(f"[{STAGE}] UBIST {period}: rows={stats[period].row_count}")
            return 0

        if dry:
            paths = discover_xlsx(
                argparse.Namespace(all=not bool(file_arg), folder=None, file=file_arg)
            )
            dry_run(paths)
            print(f"[{STAGE}] UBIST dry-run 완료 files={len(paths)}")
            return 0

        if not file_arg and not source_files and source_dir is None:
            print(f"[{STAGE}] UBIST 실패: non-dry UBIST requires explicit source files")
            return 2

        paths = list(source_files) or None
        if source_dir is not None:
            paths = sorted(
                path.resolve()
                for path in source_dir.rglob("*.xlsx")
                if path.is_file() and not path.name.startswith("~$")
            )
            if not paths:
                raise FileNotFoundError(f"no UBIST xlsx files under {source_dir}")

        stats = run_ubist_load(
            target=target,
            mode=mode,
            truncate=True,
            paths=paths,
            file=Path(str(file_arg)) if file_arg else None,
            all_sources=False,
            exclude_periods=exclude_periods,
        )
    except Exception as exc:
        print(f"[{STAGE}] UBIST 실패: {exc}")
        return 1

    total_rows = sum(stat.row_count for stat in stats.values())
    print(f"[{STAGE}] UBIST parquet load 완료 target={target} partitions={len(stats)} rows={total_rows}")
    for period in sorted(stats):
        print(f"[{STAGE}] UBIST {period}: rows={stats[period].row_count}")
    return 0


def _run_iqvia(params: dict[str, Any]) -> int:
    target_db = params.get("target_db")
    dry = bool(params.get("dry_run"))
    batch_size = int(params.get("batch_size") or 10000)
    record_parquet_dir = Path(params["record_parquet_dir"]) if params.get("record_parquet_dir") else Path("/tmp/iqvia_record_parquet_s1")
    nsa_parquet_dir = Path(params["iqvia_nsa_dir"]) if params.get("iqvia_nsa_dir") else None
    source_db = params.get("source_db") or "jw_mart"
    file_arg = params.get("file")
    source_dir = Path(str(params["iqvia_source_dir"])) if params.get("iqvia_source_dir") else None
    try:
        if source_dir is not None:
            role_bound = bind_iqvia_sources(source_dir)
            files = [canonical_nsa_source(role_bound).path]
        elif file_arg and (params.get("source") == "iqvia" or "IQVIA" in Path(str(file_arg)).parts):
            files = [Path(str(file_arg)).resolve()]
        else:
            files = iqvia_loader.discover_files()
        if dry:
            iqvia_loader.dry_run(files, None)
            print(f"[{STAGE}] IQVIA NSA dry-run 완료 files={len(files)}")
            return 0
        if not target_db:
            print(f"[{STAGE}] IQVIA 실패: --target-db is required for IQVIA run.py integration")
            return 2

        iqvia_loader.init_target_schema(str(target_db), str(source_db))
        if nsa_parquet_dir is not None:
            iqvia_loader.materialize_iqvia_nsa_parquet(files, nsa_parquet_dir)
        written = iqvia_loader.materialize_record_parquet(
            files,
            record_parquet_dir,
            batch_size=batch_size,
            overwrite=True,
        )
        loaded_rows = iqvia_loader.load_record_parquet_source(
            record_parquet_dir,
            target_database=str(target_db),
            batch_size=batch_size,
        )
    except Exception as exc:
        print(f"[{STAGE}] IQVIA 실패: {exc}")
        return 1

    print(
        f"[{STAGE}] IQVIA NSA load 완료 target_db={target_db} "
        f"partitions={len(written)} parquet_rows={sum(written.values())} rows={loaded_rows}"
    )
    return 0


def run(params: dict[str, Any]) -> int:
    source = str(params.get("source") or "all")
    if source not in VALID_SOURCES:
        print(f"[{STAGE}] 실패: unknown source={source!r}")
        return 1

    if (
        source in {"ubist", "all"}
        and not params.get("dry_run")
        and not params.get("file")
        and not params.get("source_files")
        and not params.get("ubist_source_dir")
        and not params.get("incremental")
    ):
        print(f"[{STAGE}] 실패: non-dry UBIST requires explicit source files")
        return 2

    if source in {"iqvia", "all"} and not params.get("dry_run") and not params.get("target_db"):
        print(f"[{STAGE}] 실패: --target-db is required when IQVIA is included")
        return 2

    if source in {"ubist", "all"}:
        rc = _run_ubist(params)
        if rc != 0:
            return rc
    if source in {"iqvia", "all"}:
        rc = _run_iqvia(params)
        if rc != 0:
            return rc
    return 0
