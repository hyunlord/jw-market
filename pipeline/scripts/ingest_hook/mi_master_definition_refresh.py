from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, assert_never

from pipeline.scripts.ingest_hook import config
from pipeline.scripts.ingest_hook.ledger import (
    STAGE_COMPLETE,
    STAGE_FAILED,
    STATUS_PUBLISH_RUNNING,
    Ledger,
    open_sqlite_ledger,
)
from pipeline.scripts.ingest_hook.mi_master_definition_candidate import candidate_payload
from pipeline.scripts.ingest_hook.mi_master_definition_contract import (
    ALLOWED_CACHE_REFRESH_TABLES,
    CATEGORY,
    STAGES,
    WORKFLOW_REF_URI,
    DefinitionRefreshIdentity,
    DefinitionRefreshRequest,
    PublishWorkspace,
    assert_complete_stage_contract,
    load_definition_request,
)
from pipeline.scripts.ingest_hook.mi_master_definition_commands import (
    PipelineRuntimeCatalogInvalidator,
    cache_refresher_from_request,
    prepare_adapters_from_request,
    publisher_from_request,
)
from pipeline.scripts.ingest_hook.mi_master_definition_interfaces import (
    DefinitionPublishAdapters,
    DefinitionPublishRequest,
    PrepareAdapters,
)


def _validate_workspace(workspace: PublishWorkspace) -> None:
    if not workspace.candidate_root.is_dir():
        raise RuntimeError(f"candidate_root is missing: {workspace.candidate_root}")
    if not workspace.backup_root.parent.is_dir():
        raise RuntimeError(f"backup_root parent is missing: {workspace.backup_root.parent}")
    if workspace.backup_root.exists():
        raise RuntimeError(f"backup_root must not already exist: {workspace.backup_root}")
    if not workspace.journal_path.is_file():
        raise RuntimeError(f"journal_path is missing: {workspace.journal_path}")


def _record_stage(
    ledger: Ledger,
    identity: DefinitionRefreshIdentity,
    stage_index: int,
    status: str,
    reason: str | None = None,
) -> None:
    ledger.record_stage(
        identity.ledger_epoch,
        CATEGORY,
        identity.catalog_diff_hash,
        run_id=identity.run_id,
        seq=stage_index + 1,
        stage=STAGES[stage_index],
        status=status,
        reason=reason,
    )


def _mark_failure(ledger: Ledger, identity: DefinitionRefreshIdentity, reason: str) -> None:
    ledger.mark_failed(identity.ledger_epoch, CATEGORY, identity.catalog_diff_hash, reason=reason)


def _run_prepare_stage(
    ledger: Ledger,
    request: DefinitionRefreshRequest,
    stage_index: int,
    action: Callable[[DefinitionRefreshRequest], None],
) -> bool:
    try:
        action(request)
    except (RuntimeError, ValueError) as exc:
        reason = f"{type(exc).__name__}: {exc}"
        _record_stage(ledger, request.identity, stage_index, STAGE_FAILED, reason)
        _mark_failure(ledger, request.identity, reason)
        return False
    _record_stage(ledger, request.identity, stage_index, STAGE_COMPLETE)
    return True


def prepare_definition_refresh_candidate(
    ledger: Ledger,
    request: DefinitionRefreshRequest,
    adapters: PrepareAdapters,
) -> int:
    request.identity.validate()
    _validate_workspace(request.workspace)
    try:
        payload = candidate_payload(request)
    except (RuntimeError, ValueError) as exc:
        reason = f"{type(exc).__name__}: {exc}"
        ledger.receive(
            request.identity.ledger_epoch,
            CATEGORY,
            request.identity.catalog_diff_hash,
            manifest_path=WORKFLOW_REF_URI,
        )
        _mark_failure(ledger, request.identity, reason)
        return 1
    ledger.receive(
        request.identity.ledger_epoch,
        CATEGORY,
        request.identity.catalog_diff_hash,
        manifest_path=WORKFLOW_REF_URI,
    )
    ledger.mark_running(
        request.identity.ledger_epoch,
        CATEGORY,
        request.identity.catalog_diff_hash,
        job_name=f"mi-master-definition-refresh-{request.identity.run_id}",
        run_id=request.identity.run_id,
    )
    actions = (
        adapters.catalog_sync,
        adapters.scope_plan,
        adapters.candidate_build,
        adapters.sigma,
        adapters.post_gate,
    )
    for index, action in enumerate(actions):
        if not _run_prepare_stage(ledger, request, index, action):
            return 1
    prepared_at = datetime.now(timezone.utc)
    ledger.mark_awaiting_approval(
        request.identity.ledger_epoch,
        CATEGORY,
        request.identity.catalog_diff_hash,
        run_id=request.identity.run_id,
        candidate=payload,
        prepared_at=prepared_at.isoformat(),
        expires_at=(prepared_at + timedelta(days=1)).isoformat(),
    )
    _record_stage(ledger, request.identity, 5, STAGE_COMPLETE)
    return 0


def _assert_candidate_matches(
    ledger: Ledger,
    request: DefinitionPublishRequest,
) -> None:
    identity = request.identity
    entry = ledger.status(identity.ledger_epoch, CATEGORY, identity.catalog_diff_hash)
    if entry is None or entry.status != STATUS_PUBLISH_RUNNING:
        status = None if entry is None else entry.status
        raise RuntimeError(f"definition refresh publish requires publish_running, got {status}")
    candidate = ledger.prepared_candidate(
        identity.ledger_epoch, CATEGORY, identity.catalog_diff_hash
    )
    candidate_request = request.definition_request or DefinitionRefreshRequest(
        identity=identity,
        workspace=request.workspace,
        market_ordinal=request.market_ordinal,
    )
    if candidate is None or candidate.build_run_id != identity.run_id:
        raise RuntimeError("definition refresh candidate identity mismatch")
    for key, value in candidate_payload(candidate_request).items():
        if candidate.payload.get(key) != value:
            raise RuntimeError(f"definition refresh candidate {key} changed after approval")


def _fail_publish(request: DefinitionPublishRequest, stage_index: int, exc: RuntimeError | ValueError) -> int:
    reason = f"{type(exc).__name__}: {exc}"
    _record_stage(request.ledger, request.identity, stage_index, STAGE_FAILED, reason)
    _mark_failure(request.ledger, request.identity, reason)
    return 1


def run_approved_definition_publish(request: DefinitionPublishRequest) -> int:
    try:
        request.identity.validate()
        _assert_candidate_matches(request.ledger, request)
        _validate_workspace(request.workspace)
    except (RuntimeError, ValueError) as exc:
        return _fail_publish(request, 6, exc)
    try:
        request.adapters.invalidator.preflight(request.identity)
    except (RuntimeError, ValueError) as exc:
        return _fail_publish(request, 8, exc)
    try:
        receipt = request.adapters.publisher.publish(request.workspace, request.identity)
        if (
            receipt.journal_path != request.workspace.journal_path
            or receipt.backup_root != request.workspace.backup_root
        ):
            raise RuntimeError("atomic publish receipt does not match candidate workspace")
    except (RuntimeError, ValueError) as exc:
        return _fail_publish(request, 6, exc)
    _record_stage(request.ledger, request.identity, 6, STAGE_COMPLETE)
    try:
        refreshed = request.adapters.cache_refresher.refresh_tables(ALLOWED_CACHE_REFRESH_TABLES)
        if tuple(refreshed) != ALLOWED_CACHE_REFRESH_TABLES:
            raise RuntimeError(f"forbidden cache refresh table: {tuple(refreshed)}")
    except (RuntimeError, ValueError) as exc:
        return _fail_publish(request, 7, exc)
    _record_stage(request.ledger, request.identity, 7, STAGE_COMPLETE)
    try:
        request.adapters.invalidator.invalidate(request.identity)
    except (RuntimeError, ValueError) as exc:
        return _fail_publish(request, 8, exc)
    _record_stage(request.ledger, request.identity, 8, STAGE_COMPLETE)
    try:
        events = request.ledger.stage_events(
            request.identity.ledger_epoch, CATEGORY, request.identity.catalog_diff_hash
        )
        assert_complete_stage_contract(events)
    except RuntimeError as exc:
        return _fail_publish(request, 8, exc)
    request.ledger.mark_complete(
        request.identity.ledger_epoch,
        CATEGORY,
        request.identity.catalog_diff_hash,
        row_counts={"mi_master_sha256": 1, "catalog_diff_hash": 1},
    )
    return 0


def _open_ledger(sqlite_path: str | None) -> Ledger:
    return open_sqlite_ledger(Path(sqlite_path)) if sqlite_path else config.open_configured_ledger()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m pipeline.scripts.ingest_hook.mi_master_definition_refresh")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("prepare", "approved-publish"):
        sub = subparsers.add_parser(command)
        sub.add_argument("--request-json", required=True)
        sub.add_argument("--ledger-sqlite")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    request_path = Path(args.request_json)
    request = load_definition_request(request_path)
    ledger = _open_ledger(args.ledger_sqlite)
    match args.command:
        case "prepare":
            return prepare_definition_refresh_candidate(
                ledger, request, prepare_adapters_from_request(request)
            )
        case "approved-publish":
            return run_approved_definition_publish(
                DefinitionPublishRequest(
                    ledger=ledger,
                    identity=request.identity,
                    workspace=request.workspace,
                    market_ordinal=request.market_ordinal,
                    definition_request=request,
                    adapters=DefinitionPublishAdapters(
                        publisher_from_request(request),
                        cache_refresher_from_request(request),
                        PipelineRuntimeCatalogInvalidator.from_request(request),
                    ),
                )
            )
        case unreachable:
            assert_never(unreachable)


if __name__ == "__main__":
    sys.exit(main())
