"""Stage s1 load - raw source dispatcher."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from pipeline.etl.io import iqvia_loader
from pipeline.etl.io.ubist_loader import TARGET_DIR, discover_xlsx, dry_run, run_ubist_load

STAGE = "s1 load"
VALID_SOURCES = {"ubist", "iqvia", "all"}


def _run_ubist(params: dict[str, Any]) -> int:
    target = Path(params["target_dir"]) if params.get("target_dir") else TARGET_DIR
    mode = params.get("ubist_mode") or "replace"
    dry = bool(params.get("dry_run"))
    file_arg = params.get("file")
    try:
        if dry:
            paths = discover_xlsx(
                argparse.Namespace(all=not bool(file_arg), folder=None, file=file_arg)
            )
            dry_run(paths)
            print(f"[{STAGE}] UBIST dry-run 완료 files={len(paths)}")
            return 0

        if not file_arg:
            print(f"[{STAGE}] UBIST 실패: non-dry UBIST requires --file to avoid full reload")
            return 2

        stats = run_ubist_load(
            target=target,
            mode=mode,
            truncate=False,
            file=Path(str(file_arg)),
            all_sources=False,
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
    source_db = params.get("source_db") or "jw_mart"
    file_arg = params.get("file")
    try:
        if file_arg and (params.get("source") == "iqvia" or "IQVIA" in Path(str(file_arg)).parts):
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

    if source in {"ubist", "all"} and not params.get("dry_run") and not params.get("file"):
        print(f"[{STAGE}] 실패: non-dry UBIST requires --file to avoid full reload")
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
