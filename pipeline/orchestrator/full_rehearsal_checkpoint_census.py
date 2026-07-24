"""Nine-gate census for immutable R-1 s1 checkpoints."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Mapping

import pyarrow.parquet as pq

from pipeline.orchestrator.full_rehearsal_checkpoint_contract import (
    CheckpointContractError,
    DatabaseCensus,
    inventory_canonical_sha,
    sha256_file,
)

if TYPE_CHECKING:
    from pipeline.orchestrator.full_rehearsal import FullInputManifest


def read_database_census(target_database: str) -> DatabaseCensus:
    from pipeline.etl.io import iqvia_loader

    connection = iqvia_loader.connect(target_database)
    try:
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*) FROM `{iqvia_loader.NSA_TABLE}`")
            row_count = int(cursor.fetchone()[0])
            cursor.execute(
                f"SELECT period_label, COUNT(*) FROM `{iqvia_loader.NSA_TABLE}` "
                "GROUP BY period_label ORDER BY period_label"
            )
            period_counts = {str(period): int(count) for period, count in cursor.fetchall()}
    finally:
        connection.close()
    return DatabaseCensus(row_count, period_counts)


def parquet_record(path: Path, base: Path) -> dict[str, object]:
    if path.stat().st_size == 0:
        raise CheckpointContractError(f"zero-byte parquet: {path}")
    try:
        rows = pq.ParquetFile(path).metadata.num_rows
    except Exception as exc:
        raise CheckpointContractError(f"invalid parquet {path}: {exc}") from exc
    return {
        "path": path.relative_to(base).as_posix(),
        "row_count": int(rows),
        "sha256": sha256_file(path),
        "size": path.stat().st_size,
    }


def artifact_records(s1_root: Path) -> list[dict[str, object]]:
    records = [
        parquet_record(path, s1_root)
        for path in sorted(s1_root.rglob("*.parquet"))
        if path.is_file()
    ]
    manifest = s1_root / "ubist" / "_manifest.json"
    if not manifest.is_file():
        raise CheckpointContractError("UBIST manifest is missing")
    records.append(
        {
            "path": manifest.relative_to(s1_root).as_posix(),
            "row_count": 0,
            "sha256": sha256_file(manifest),
            "size": manifest.stat().st_size,
        }
    )
    return sorted(records, key=lambda row: str(row["path"]))


def _sidecar_values(sidecar: object) -> tuple[Path, str]:
    relative = getattr(sidecar, "relative_path", None)
    sha256 = getattr(sidecar, "sha256", None)
    if relative is None and isinstance(sidecar, Mapping):
        relative = sidecar.get("relative_path")
        sha256 = sidecar.get("sha256")
    if not isinstance(relative, (str, Path)) or not isinstance(sha256, str):
        raise CheckpointContractError("invalid expected sidecar contract")
    return Path(relative), sha256


def checkpoint_census(
    *,
    work: Path,
    inventory_path: Path,
    input_manifest: FullInputManifest,
    database: DatabaseCensus,
    expected_sidecars: Iterable[object],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    for name in ("ubist", "iqvia-records", "iqvia-nsa"):
        if not (work / name).is_dir():
            raise CheckpointContractError(f"missing s1 artifact directory: {name}")

    from pipeline.orchestrator.full_rehearsal_preflight import check_inventory

    inventory_finding = check_inventory(input_manifest, inventory_path)
    if not inventory_finding.passed:
        raise CheckpointContractError(
            f"input inventory changed before checkpoint publish: {inventory_finding.detail}"
        )
    census: list[dict[str, object]] = [
        {
            "check": "1-input-inventory",
            "passed": True,
            "detail": (
                f"canonical_sha={inventory_canonical_sha(inventory_path)} "
                f"{inventory_finding.detail}"
            ),
        }
    ]

    manifest_path = work / "ubist" / "_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        partitions = manifest["partitions"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise CheckpointContractError(f"invalid UBIST manifest: {exc}") from exc
    expected = {str(row["path"]): int(row["row_count"]) for row in partitions}
    actual_ubist = {
        path.relative_to(work / "ubist").as_posix(): pq.ParquetFile(path).metadata.num_rows
        for path in sorted((work / "ubist").rglob("*.parquet"))
    }
    if expected != actual_ubist:
        raise CheckpointContractError("UBIST partition set or row counts mismatch")
    census.append(
        {"check": "2-ubist-partitions", "passed": True, "detail": f"partitions={len(expected)}"}
    )

    source_artifacts = artifact_records(work)
    census.append(
        {"check": "3-parquet-identities", "passed": True, "detail": f"files={len(source_artifacts)}"}
    )
    census.append(
        {
            "check": "4-ubist-manifest",
            "passed": True,
            "detail": f"sha256={sha256_file(manifest_path)} rows={sum(expected.values())}",
        }
    )
    temporary = [
        path
        for path in work.rglob("*")
        if path.name.startswith((".__tmp_", ".__backup_"))
    ]
    if temporary:
        raise CheckpointContractError(f"temporary artifacts remain: {len(temporary)}")
    census.append({"check": "5-temporary-artifacts", "passed": True, "detail": "count=0"})

    record_artifacts = [
        row for row in source_artifacts if str(row["path"]).startswith("iqvia-records/")
    ]
    nsa_artifacts = [
        row for row in source_artifacts if str(row["path"]).startswith("iqvia-nsa/")
    ]
    if not record_artifacts or not nsa_artifacts:
        raise CheckpointContractError("IQVIA record/NSA parquet population is incomplete")
    census.append(
        {
            "check": "6-iqvia-parquet",
            "passed": True,
            "detail": f"record={len(record_artifacts)} nsa={len(nsa_artifacts)}",
        }
    )
    record_periods = {
        Path(str(row["path"])).stem: int(row["row_count"]) for row in record_artifacts
    }
    record_rows = sum(record_periods.values())
    if database.row_count != record_rows:
        raise CheckpointContractError(
            f"DB row count mismatch: db={database.row_count} parquet={record_rows}"
        )
    if dict(database.period_counts) != record_periods:
        raise CheckpointContractError(
            f"DB period distribution mismatch: db={dict(database.period_counts)} parquet={record_periods}"
        )
    census.append(
        {"check": "7-iqvia-database", "passed": True, "detail": f"rows={record_rows}"}
    )

    sidecar_count = 0
    for expected_sidecar in expected_sidecars:
        relative, sha256 = _sidecar_values(expected_sidecar)
        path = work / "ubist" / relative
        if not path.is_file() or sha256_file(path) != sha256:
            raise CheckpointContractError(f"sidecar identity mismatch: {relative}")
        sidecar_count += 1
    census.append(
        {"check": "8-sidecar-contract", "passed": True, "detail": f"count={sidecar_count}"}
    )
    return census, source_artifacts
