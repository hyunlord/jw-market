"""Checked-in local MI Master definition-refresh adapters."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from pipeline.scripts.ingest_hook.mi_master_definition_contract import (
    DefinitionRefreshIdentity,
    DefinitionRefreshRequest,
    PublishReceipt,
    PublishWorkspace,
)


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def _artifact(request: DefinitionRefreshRequest, stage: str) -> Path:
    return request.workspace.candidate_root / f"{stage}.json"


def _require_artifact(request: DefinitionRefreshRequest, stage: str) -> None:
    path = _artifact(request, stage)
    if not path.is_file():
        raise RuntimeError(f"NOT_IMPLEMENTED: {stage} adapter artifact missing")


def catalog_sync(request: DefinitionRefreshRequest) -> None:
    _write_json(
        _artifact(request, "catalog_sync"),
        {
            "stage": "catalog_sync",
            "identity": request.identity.as_dict(),
            "market_ordinal": request.market_ordinal,
            "executed_at": _timestamp(),
        },
    )


def scope_plan(request: DefinitionRefreshRequest) -> None:
    _require_artifact(request, "catalog_sync")
    _write_json(
        _artifact(request, "scope_plan"),
        {
            "stage": "scope_plan",
            "market_ordinal": request.market_ordinal,
            "scope": "definition_refresh",
            "executed_at": _timestamp(),
        },
    )


def candidate_build(request: DefinitionRefreshRequest) -> None:
    _require_artifact(request, "scope_plan")
    _write_json(
        _artifact(request, "candidate_build"),
        {
            "stage": "candidate_build",
            "identity": request.identity.as_dict(),
            "workspace": request.workspace.as_dict(),
            "executed_at": _timestamp(),
        },
    )


def sigma(request: DefinitionRefreshRequest) -> None:
    _require_artifact(request, "candidate_build")
    request.identity.validate()
    _write_json(
        _artifact(request, "sigma"),
        {
            "stage": "sigma",
            "mi_master_sha256": request.identity.mi_master_sha256,
            "catalog_diff_hash": request.identity.catalog_diff_hash,
            "executed_at": _timestamp(),
        },
    )


def post_gate(request: DefinitionRefreshRequest) -> None:
    for stage in ("catalog_sync", "scope_plan", "candidate_build", "sigma"):
        _require_artifact(request, stage)
    _write_json(
        _artifact(request, "post_gate"),
        {
            "stage": "post_gate",
            "ready_for_approval": True,
            "executed_at": _timestamp(),
        },
    )


@dataclass(frozen=True, slots=True)
class LocalPrepareAdapters:
    def catalog_sync(self, request: DefinitionRefreshRequest) -> None:
        catalog_sync(request)

    def scope_plan(self, request: DefinitionRefreshRequest) -> None:
        scope_plan(request)

    def candidate_build(self, request: DefinitionRefreshRequest) -> None:
        candidate_build(request)

    def sigma(self, request: DefinitionRefreshRequest) -> None:
        sigma(request)

    def post_gate(self, request: DefinitionRefreshRequest) -> None:
        post_gate(request)


@dataclass(frozen=True, slots=True)
class LocalPublisher:
    def publish(
        self, workspace: PublishWorkspace, identity: DefinitionRefreshIdentity
    ) -> PublishReceipt:
        workspace.backup_root.mkdir()
        _write_json(
            workspace.backup_root / "publish_receipt.json",
            {
                "stage": "mart_publish",
                "identity": identity.as_dict(),
                "journal_path": str(workspace.journal_path),
                "executed_at": _timestamp(),
            },
        )
        return PublishReceipt(workspace.journal_path, workspace.backup_root)


@dataclass(frozen=True, slots=True)
class LocalCacheRefresher:
    candidate_root: Path

    def refresh_tables(self, tables: tuple[str, ...]) -> tuple[str, ...]:
        _write_json(
            self.candidate_root / "cache_refresh.json",
            {"stage": "cache_refresh", "tables": list(tables), "executed_at": _timestamp()},
        )
        return tables


@dataclass(frozen=True, slots=True)
class LocalRuntimeCatalogInvalidator:
    candidate_root: Path

    def invalidate(self, identity: DefinitionRefreshIdentity) -> None:
        _write_json(
            self.candidate_root / "catalog_invalidate.json",
            {
                "stage": "catalog_invalidate",
                "identity": identity.as_dict(),
                "executed_at": _timestamp(),
            },
        )
