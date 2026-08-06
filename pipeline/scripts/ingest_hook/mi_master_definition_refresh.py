"""MI Master definition-refresh publish slice.

This module is intentionally separate from the upload-manifest runner. A MI
Master definition change is identified by the source workbook hash, the catalog
diff hash, and the run id that prepared the candidate. It reuses the existing
ledger state machine, but it does not pretend the definition change is a UBIST
or IQVIA upload manifest.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from pipeline.scripts.ingest_hook.ledger import (
    STAGE_COMPLETE,
    STATUS_PUBLISH_RUNNING,
    Ledger,
    StageEvent,
)

CATEGORY = "mi_master_definition"
MANIFEST_PATH_SENTINEL = "definition-refresh"
ALLOWED_CACHE_REFRESH_TABLES = ("cache_brands", "cache_market_status")
STAGES = (
    "identity",
    "catalog_diff",
    "candidate_preflight",
    "backup_preflight",
    "journal_preflight",
    "awaiting_approval",
    "atomic_publish",
    "cache_refresh",
    "catalog_invalidate",
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class MissingStageError(RuntimeError):
    """The source-specific MI Master progress contract is incomplete."""

    def __init__(self, missing: tuple[str, ...]):
        self.missing = missing
        joined = ", ".join(missing)
        super().__init__(f"missing MI Master definition refresh stages: {joined}")


@dataclass(frozen=True, slots=True)
class DefinitionRefreshIdentity:
    mi_master_sha256: str
    catalog_diff_hash: str
    run_id: str

    @property
    def ledger_epoch(self) -> str:
        return f"mi-master-{self.mi_master_sha256[:12]}"

    def validate(self) -> None:
        for label, value in (
            ("mi_master_sha256", self.mi_master_sha256),
            ("catalog_diff_hash", self.catalog_diff_hash),
        ):
            if _SHA256_RE.fullmatch(value) is None:
                raise RuntimeError(f"{label} must be a lowercase sha256 hex digest")
        if not self.run_id.strip():
            raise RuntimeError("run_id is required")


@dataclass(frozen=True, slots=True)
class PublishWorkspace:
    candidate_root: Path
    backup_root: Path
    journal_path: Path


@dataclass(frozen=True, slots=True)
class PublishReceipt:
    journal_path: Path
    backup_root: Path


@dataclass(frozen=True, slots=True)
class DefinitionPublishAdapters:
    publisher: AtomicPublishOrchestrator
    cache_refresher: CacheRefresher
    invalidator: RuntimeCatalogInvalidator


@dataclass(frozen=True, slots=True)
class DefinitionPublishRequest:
    ledger: Ledger
    identity: DefinitionRefreshIdentity
    workspace: PublishWorkspace
    adapters: DefinitionPublishAdapters


class AtomicPublishOrchestrator(Protocol):
    def publish(
        self, workspace: PublishWorkspace, identity: DefinitionRefreshIdentity
    ) -> PublishReceipt:
        """Atomically publish the prepared catalog candidate."""


class CacheRefresher(Protocol):
    def refresh_tables(self, tables: tuple[str, ...]) -> tuple[str, ...]:
        """Refresh exactly the cache tables requested by this slice."""


class RuntimeCatalogInvalidator(Protocol):
    def invalidate(self, identity: DefinitionRefreshIdentity) -> None:
        """Invalidate serving-runtime catalog state after publish."""


def _record_complete_stage(
    ledger: Ledger,
    identity: DefinitionRefreshIdentity,
    stage_index: int,
) -> None:
    stage = STAGES[stage_index]
    ledger.record_stage(
        identity.ledger_epoch,
        CATEGORY,
        identity.catalog_diff_hash,
        run_id=identity.run_id,
        seq=stage_index + 1,
        stage=stage,
        status=STAGE_COMPLETE,
    )


def _validate_workspace(workspace: PublishWorkspace) -> None:
    if not workspace.candidate_root.is_dir():
        raise RuntimeError(f"candidate_root is missing: {workspace.candidate_root}")
    if not workspace.backup_root.parent.is_dir():
        raise RuntimeError(
            f"backup_root parent is missing: {workspace.backup_root.parent}"
        )
    if workspace.backup_root.exists():
        raise RuntimeError(f"backup_root must not already exist: {workspace.backup_root}")
    if not workspace.journal_path.is_file():
        raise RuntimeError(f"journal_path is missing: {workspace.journal_path}")


def _candidate_payload(
    identity: DefinitionRefreshIdentity, workspace: PublishWorkspace
) -> dict[str, str | list[str] | dict[str, str]]:
    return {
        "identity": {
            "mi_master_sha256": identity.mi_master_sha256,
            "catalog_diff_hash": identity.catalog_diff_hash,
            "run_id": identity.run_id,
        },
        "candidate_root": str(workspace.candidate_root),
        "backup_root": str(workspace.backup_root),
        "journal_path": str(workspace.journal_path),
        "cache_refresh_tables": list(ALLOWED_CACHE_REFRESH_TABLES),
        "runtime_catalog_invalidation": "required",
    }


def prepare_definition_refresh_candidate(
    ledger: Ledger,
    identity: DefinitionRefreshIdentity,
    workspace: PublishWorkspace,
) -> None:
    """Register a definition-refresh candidate and pause at explicit approval."""
    identity.validate()
    _validate_workspace(workspace)
    ledger.receive(
        identity.ledger_epoch,
        CATEGORY,
        identity.catalog_diff_hash,
        manifest_path=MANIFEST_PATH_SENTINEL,
    )
    ledger.mark_running(
        identity.ledger_epoch,
        CATEGORY,
        identity.catalog_diff_hash,
        job_name=f"mi-master-definition-refresh-{identity.run_id}",
        run_id=identity.run_id,
    )
    for stage_index in range(5):
        _record_complete_stage(ledger, identity, stage_index)
    prepared_at = datetime.now(timezone.utc)
    expires_at = prepared_at + timedelta(days=1)
    ledger.mark_awaiting_approval(
        identity.ledger_epoch,
        CATEGORY,
        identity.catalog_diff_hash,
        run_id=identity.run_id,
        candidate=_candidate_payload(identity, workspace),
        prepared_at=prepared_at.isoformat(),
        expires_at=expires_at.isoformat(),
    )
    _record_complete_stage(ledger, identity, 5)


def _assert_candidate_matches(
    ledger: Ledger,
    identity: DefinitionRefreshIdentity,
    workspace: PublishWorkspace,
) -> None:
    entry = ledger.status(identity.ledger_epoch, CATEGORY, identity.catalog_diff_hash)
    if entry is None:
        raise RuntimeError("definition refresh ledger entry is missing")
    if entry.status != STATUS_PUBLISH_RUNNING:
        raise RuntimeError(
            f"definition refresh publish requires {STATUS_PUBLISH_RUNNING}, got {entry.status}"
        )
    candidate = ledger.prepared_candidate(
        identity.ledger_epoch, CATEGORY, identity.catalog_diff_hash
    )
    if candidate is None or candidate.build_run_id != identity.run_id:
        raise RuntimeError("definition refresh candidate identity mismatch")
    expected = _candidate_payload(identity, workspace)
    for key, value in expected.items():
        if candidate.payload.get(key) != value:
            raise RuntimeError(
                f"definition refresh candidate {key} changed after approval"
            )


def _assert_cache_scope(refreshed: tuple[str, ...]) -> None:
    if set(refreshed) != set(ALLOWED_CACHE_REFRESH_TABLES):
        raise RuntimeError(
            "forbidden cache refresh table in MI Master definition refresh: "
            f"{tuple(refreshed)}"
        )


def run_approved_definition_publish(request: DefinitionPublishRequest) -> int:
    """Publish an approved MI Master definition candidate.

    The orchestration is deliberately dependency-injected so this slice defines
    the publish interfaces without executing DDL, deployment, or production DB
    mutations on import or in tests.
    """
    identity = request.identity
    workspace = request.workspace
    identity.validate()
    _assert_candidate_matches(request.ledger, identity, workspace)
    _validate_workspace(workspace)
    receipt = request.adapters.publisher.publish(workspace, identity)
    if (
        receipt.journal_path != workspace.journal_path
        or receipt.backup_root != workspace.backup_root
    ):
        raise RuntimeError("atomic publish receipt does not match candidate workspace")
    _record_complete_stage(request.ledger, identity, 6)
    refreshed = request.adapters.cache_refresher.refresh_tables(
        ALLOWED_CACHE_REFRESH_TABLES
    )
    _assert_cache_scope(refreshed)
    _record_complete_stage(request.ledger, identity, 7)
    request.adapters.invalidator.invalidate(identity)
    _record_complete_stage(request.ledger, identity, 8)
    request.ledger.mark_complete(
        identity.ledger_epoch,
        CATEGORY,
        identity.catalog_diff_hash,
        row_counts={
            "mi_master_sha256": 1,
            "catalog_diff_hash": 1,
        },
    )
    return 0


def assert_complete_stage_contract(events: list[StageEvent]) -> None:
    completed = {event.stage for event in events if event.status == STAGE_COMPLETE}
    missing = tuple(stage for stage in STAGES if stage not in completed)
    if missing:
        raise MissingStageError(missing)
