from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import time
from typing import Any, Mapping

from pipeline.scripts.deploy.mart_load_ops import (
    MART_TABLES,
    PROTECTED_TARGETS,
    STRATEGIC_MARKET_TABLE,
    connect_admin,
    dump_tables,
    guard_run,
    run_bridge,
    run_s4_general,
    run_strategic_ml_market_from_source,
)
from pipeline.scripts.deploy.mart_import_ops import (
    attach_dump_to_manifest,
    capture_manifest,
    drop_schema_if_unprotected,
    ensure_direct_import_target_absent,
    ensure_schema_exists,
    manifest_entries,
    restore_dump_into_schema,
    sha256_file,
    verify_against_manifest,
)


@dataclass(frozen=True, slots=True)
class DirectBuildImportConfig:
    run_id: str
    source_db: str
    target_db: str
    build_db: str
    dump_path: Path
    manifest_json: Path
    audit_json: Path | None
    catalog_root: Path | None
    input_mode: str
    include_strategic_ml_market: bool
    allow_operating_target: bool
    create_target_db: bool
    drop_build_after_dump: bool
    drop_target_after_verify: bool


@dataclass(frozen=True, slots=True)
class DumpImportConfig:
    target_db: str
    dump_path: Path
    manifest_json: Path
    audit_json: Path | None
    allow_operating_target: bool
    create_target_db: bool
    drop_target_after_verify: bool


def direct_import_tables(include_strategic_ml_market: bool) -> tuple[str, ...]:
    if include_strategic_ml_market:
        return (*MART_TABLES, STRATEGIC_MARKET_TABLE)
    return MART_TABLES


def run_build_dump_import(config: DirectBuildImportConfig) -> dict[str, Any]:
    started = time.perf_counter()
    tables = direct_import_tables(config.include_strategic_ml_market)
    guard_run(
        source_db=config.source_db,
        target_db=config.target_db,
        build_db=config.build_db,
        allow_operating_target=config.allow_operating_target,
    )
    _prepare_import_target(config.target_db, tables, config.create_target_db)
    build_start = time.perf_counter()
    run_s4_general(
        build_db=config.build_db,
        source_db=config.source_db,
        catalog_root=config.catalog_root,
        input_mode=config.input_mode,
    )
    if config.include_strategic_ml_market:
        run_strategic_ml_market_from_source(
            build_db=config.build_db,
            source_db=config.source_db,
            catalog_root=config.catalog_root,
        )
    run_bridge(build_db=config.build_db, source_db=config.source_db, catalog_root=config.catalog_root)
    build_seconds = time.perf_counter() - build_start
    conn = connect_admin()
    try:
        manifest = capture_manifest(conn, run_id=config.run_id, source_db=config.source_db, build_db=config.build_db, tables=tables)
    finally:
        conn.close()
    dump_result = dump_tables(target_db=config.build_db, tables=tables, dump_path=config.dump_path)
    manifest = attach_dump_to_manifest(manifest, dump_path=config.dump_path, dump_seconds=dump_result.elapsed_seconds)
    _write_json(config.manifest_json, manifest)
    if config.drop_build_after_dump:
        conn = connect_admin()
        try:
            drop_schema_if_unprotected(conn, config.build_db)
        finally:
            conn.close()
    restore_result = restore_dump_into_schema(target_db=config.target_db, dump_path=config.dump_path)
    verify_start = time.perf_counter()
    conn = connect_admin()
    try:
        verification = verify_against_manifest(conn, target_db=config.target_db, manifest=manifest)
        if config.drop_target_after_verify:
            drop_schema_if_unprotected(conn, config.target_db)
    finally:
        conn.close()
    summary = {
        "mode": "direct_build_dump_import",
        "run_id": config.run_id,
        "source_db": config.source_db,
        "target_db": config.target_db,
        "build_db": config.build_db,
        "tables": list(tables),
        "build_seconds": round(build_seconds, 3),
        "dump_seconds": round(dump_result.elapsed_seconds, 3),
        "restore_seconds": round(restore_result.elapsed_seconds, 3),
        "verify_seconds": round(time.perf_counter() - verify_start, 3),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "manifest": str(config.manifest_json),
        "dump": manifest["dump"],
        "verification": verification,
        "cleanup": {
            "build_db_dropped": config.drop_build_after_dump,
            "target_db_dropped": config.drop_target_after_verify,
        },
    }
    if config.audit_json:
        _write_json(config.audit_json, summary)
    return summary


def run_dump_import(config: DumpImportConfig) -> dict[str, Any]:
    started = time.perf_counter()
    manifest = json.loads(config.manifest_json.read_text(encoding="utf-8"))
    tables = tuple(str(entry["table"]) for entry in manifest_entries(manifest))
    if config.target_db in PROTECTED_TARGETS and not config.allow_operating_target:
        raise RuntimeError("refusing operating target import without --allow-operating-target")
    _prepare_import_target(config.target_db, tables, config.create_target_db)
    restore_result = restore_dump_into_schema(target_db=config.target_db, dump_path=config.dump_path)
    conn = connect_admin()
    try:
        verification = verify_against_manifest(conn, target_db=config.target_db, manifest=manifest)
        if config.drop_target_after_verify:
            drop_schema_if_unprotected(conn, config.target_db)
    finally:
        conn.close()
    summary = {
        "mode": "dump_import",
        "target_db": config.target_db,
        "tables": list(tables),
        "restore_seconds": round(restore_result.elapsed_seconds, 3),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "manifest": str(config.manifest_json),
        "dump": {
            "path": str(config.dump_path),
            "size_bytes": restore_result.size_bytes,
            "sha256": sha256_file(config.dump_path),
        },
        "verification": verification,
        "cleanup": {"target_db_dropped": config.drop_target_after_verify},
    }
    if config.audit_json:
        _write_json(config.audit_json, summary)
    return summary


def _prepare_import_target(target_db: str, tables: tuple[str, ...], create_target_db: bool) -> None:
    conn = connect_admin()
    try:
        ensure_schema_exists(conn, target_db, create=create_target_db)
        ensure_direct_import_target_absent(conn, target_db=target_db, tables=tables)
    finally:
        conn.close()


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
