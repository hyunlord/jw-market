"""Provenance, approval, and seed-contract gates."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Mapping, Sequence

from pipeline.etl.mi_master_refresh.contracts import (
    LIVE_CATALOG_TABLES,
    STRATEGIC_REFRESH_TABLES,
    SUPPORTED_REFRESH_CACHE_TABLES,
    CandidateSeedContract,
    CatalogTableSnapshot,
    DefinitionApprovalIdentity,
    MiMasterRefreshCandidate,
)
from pipeline.etl.mi_master_refresh.utils import require_sha256


def mi_master_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_candidate_seed(path: Path) -> MiMasterRefreshCandidate:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"candidate seed is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("candidate seed must be a JSON object")
    tables = payload.get("allowed_cache_tables")
    if not isinstance(tables, list):
        raise ValueError("candidate seed allowed_cache_tables must be a list")
    return MiMasterRefreshCandidate(
        candidate_id=str(payload.get("candidate_id") or ""),
        mi_master_sha256=str(payload.get("mi_master_sha256") or ""),
        manifest_sha256=str(payload.get("manifest_sha256") or ""),
        allowed_cache_tables=tuple(str(table) for table in tables),
    )


def validate_candidate_seed(candidate: MiMasterRefreshCandidate) -> None:
    if not candidate.candidate_id.strip():
        raise ValueError("candidate_id is required")
    require_sha256(candidate.mi_master_sha256, "mi_master_sha256")
    require_sha256(candidate.manifest_sha256, "manifest_sha256")
    unsupported = sorted(
        set(candidate.allowed_cache_tables) - set(SUPPORTED_REFRESH_CACHE_TABLES)
    )
    if unsupported:
        raise ValueError(f"unsupported cache table for MI Master refresh: {unsupported}")


def validate_definition_approval(
    candidate: MiMasterRefreshCandidate,
    payload: Mapping[str, object],
    *,
    expected: DefinitionApprovalIdentity,
) -> None:
    validate_candidate_approval_identity(candidate, expected)
    required: dict[str, object] = {"approved": True, **expected.as_dict()}
    for field, value in required.items():
        if payload.get(field) != value:
            raise ValueError(f"definition approval {field} does not match")


def validate_candidate_approval_identity(
    candidate: MiMasterRefreshCandidate,
    identity: DefinitionApprovalIdentity,
) -> None:
    if identity.mi_master_sha256 != candidate.mi_master_sha256:
        raise ValueError("definition approval mi_master_sha256 does not match candidate")
    if identity.catalog_diff_hash != candidate.manifest_sha256:
        raise ValueError("definition approval catalog_diff_hash does not match candidate")
    if identity.run_id != candidate.candidate_id:
        raise ValueError("definition approval run_id does not match candidate")


def validate_manifest_equality(
    candidate_hashes: Mapping[str, str],
    serving_hashes: Mapping[str, str],
) -> None:
    tables = sorted(set(candidate_hashes) | set(serving_hashes))
    mismatched = [
        table
        for table in tables
        if candidate_hashes.get(table) != serving_hashes.get(table)
    ]
    if mismatched:
        raise ValueError("catalog manifest equality failed: " + ", ".join(mismatched))


def validate_candidate_seed_contract(contract: CandidateSeedContract) -> None:
    _validate_snapshot_group(
        contract.live_catalog,
        required=LIVE_CATALOG_TABLES,
        label="live catalog",
    )
    _validate_snapshot_group(
        contract.strategic_tables,
        required=STRATEGIC_REFRESH_TABLES,
        label="strategic tables",
    )


def _validate_snapshot_group(
    snapshots: Sequence[CatalogTableSnapshot],
    *,
    required: Sequence[str],
    label: str,
) -> None:
    by_table = {snapshot.table_name: snapshot for snapshot in snapshots}
    missing = [table for table in required if table not in by_table]
    if missing:
        raise ValueError(f"{label} missing snapshots: {', '.join(missing)}")
    for table in required:
        snapshot = by_table[table]
        if snapshot.row_count < 0:
            raise ValueError(f"{table} row_count must be non-negative")
        require_sha256(snapshot.sha256, f"{table}.sha256")
