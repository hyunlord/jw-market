"""Candidate payload binding for MI Master definition refresh."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from pipeline.scripts.ingest_hook.mi_master_definition_contract import (
    ALLOWED_CACHE_REFRESH_TABLES,
    WORKFLOW_REF_URI,
    DefinitionRefreshRequest,
)


def candidate_payload(request: DefinitionRefreshRequest) -> dict[str, object]:
    _validate_publish_plan_binding(request)
    payload: dict[str, object] = {
        "identity": request.identity.as_dict(),
        "candidate_root": str(request.workspace.candidate_root),
        "backup_root": str(request.workspace.backup_root),
        "journal_path": str(request.workspace.journal_path),
        "cache_refresh_tables": list(ALLOWED_CACHE_REFRESH_TABLES),
        "market_ordinal": request.market_ordinal,
        "catalog_sync": None if request.catalog_sync is None else request.catalog_sync.as_dict(),
        "runtime_catalog_invalidation": "required",
        "workflow_ref": WORKFLOW_REF_URI,
    }
    if request.publish_plan is not None:
        payload["publish_plan"] = dict(request.publish_plan)
    if request.post_gate is not None:
        payload["post_gate"] = {
            key: request.post_gate[key]
            for key in ("candidate", "seed_contract")
            if key in request.post_gate
        }
    return payload


def _validate_publish_plan_binding(request: DefinitionRefreshRequest) -> None:
    if request.publish_plan is None:
        return
    plan = request.publish_plan
    candidate = _mapping(plan, "candidate")
    corpus = _mapping(plan, "corpus")
    approval = _mapping(plan, "approval_identity")
    candidate_id = str(candidate.get("candidate_id") or "")
    expected_approval = request.identity.as_dict()
    expected = {
        "candidate_dir": request.workspace.candidate_root,
        "journal_path": request.workspace.journal_path,
        "backup_root": request.workspace.backup_root,
        "corpus_candidate_dir": request.workspace.candidate_root,
    }
    actual = {
        "candidate_dir": Path(str(plan.get("candidate_dir") or "")),
        "journal_path": Path(str(plan.get("journal_path") or "")),
        "backup_root": Path(str(plan.get("backup_dir") or "")) / candidate_id,
        "corpus_candidate_dir": Path(str(corpus.get("candidate_dir") or "")),
    }
    for key, value in expected.items():
        if actual[key] != value:
            raise RuntimeError(f"publish_plan {key} does not match workspace")
    if Path(str(corpus.get("backup_dir") or "")) != Path(str(plan.get("backup_dir") or "")):
        raise RuntimeError("publish_plan corpus backup_dir does not match backup_dir")
    if approval != expected_approval:
        raise RuntimeError("publish_plan approval identity does not match request")
    if candidate.get("mi_master_sha256") != request.identity.mi_master_sha256:
        raise RuntimeError("publish_plan candidate mi_master_sha256 does not match request")
    if candidate.get("manifest_sha256") != request.identity.catalog_diff_hash:
        raise RuntimeError("publish_plan candidate manifest_sha256 does not match request")


def _mapping(payload: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise RuntimeError(f"publish_plan {key} must be an object")
    return value
