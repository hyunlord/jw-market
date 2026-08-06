from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from pipeline.scripts.ingest_hook.ledger import (
    STATUS_AWAITING_APPROVAL,
    STATUS_COMPLETE,
    STATUS_PUBLISH_RUNNING,
)
from pipeline.scripts.ingest_hook.mi_master_definition_refresh import (
    ALLOWED_CACHE_REFRESH_TABLES,
    CATEGORY,
    DefinitionPublishAdapters,
    DefinitionPublishRequest,
    DefinitionRefreshIdentity,
    MissingStageError,
    PublishReceipt,
    PublishWorkspace,
    assert_complete_stage_contract,
    prepare_definition_refresh_candidate,
    run_approved_definition_publish,
)


@dataclass
class RecordingPublisher:
    calls: list[PublishWorkspace]

    def publish(
        self, workspace: PublishWorkspace, identity: DefinitionRefreshIdentity
    ) -> PublishReceipt:
        self.calls.append(workspace)
        return PublishReceipt(
            journal_path=workspace.journal_path,
            backup_root=workspace.backup_root,
        )


@dataclass
class RecordingCacheRefresher:
    requested: list[tuple[str, ...]]
    returned: tuple[str, ...] = ALLOWED_CACHE_REFRESH_TABLES

    def refresh_tables(self, tables: tuple[str, ...]) -> tuple[str, ...]:
        self.requested.append(tables)
        return self.returned


@dataclass
class RecordingInvalidator:
    identities: list[DefinitionRefreshIdentity]

    def invalidate(self, identity: DefinitionRefreshIdentity) -> None:
        self.identities.append(identity)


def _identity() -> DefinitionRefreshIdentity:
    return DefinitionRefreshIdentity(
        mi_master_sha256="a" * 64,
        catalog_diff_hash="b" * 64,
        run_id="run-mi-master-1",
    )


def _workspace(tmp_path: Path) -> PublishWorkspace:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    backup = tmp_path / "backup"
    journal = tmp_path / "journal.json"
    journal.write_text("{}", encoding="utf-8")
    return PublishWorkspace(
        candidate_root=candidate,
        backup_root=backup,
        journal_path=journal,
    )


def _request(
    sqlite_ledger,
    identity: DefinitionRefreshIdentity,
    workspace: PublishWorkspace,
    *,
    publisher: RecordingPublisher | None = None,
    cache_refresher: RecordingCacheRefresher | None = None,
    invalidator: RecordingInvalidator | None = None,
) -> DefinitionPublishRequest:
    return DefinitionPublishRequest(
        ledger=sqlite_ledger,
        identity=identity,
        workspace=workspace,
        adapters=DefinitionPublishAdapters(
            publisher=publisher or RecordingPublisher([]),
            cache_refresher=cache_refresher or RecordingCacheRefresher([]),
            invalidator=invalidator or RecordingInvalidator([]),
        ),
    )


def test_prepare_uses_definition_identity_without_fake_upload_manifest(
    sqlite_ledger, tmp_path: Path
) -> None:
    # Given: a MI Master definition change identified by content and catalog diff hashes.
    identity = _identity()
    workspace = _workspace(tmp_path)

    # When: the candidate is prepared for approval.
    prepare_definition_refresh_candidate(sqlite_ledger, identity, workspace)

    # Then: the ledger identity is the definition-change identity, not an upload manifest.
    entry = sqlite_ledger.status(
        identity.ledger_epoch, CATEGORY, identity.catalog_diff_hash
    )
    assert entry is not None
    assert entry.status == STATUS_AWAITING_APPROVAL
    assert entry.manifest_path == "definition-refresh"
    assert entry.run_id == identity.run_id
    candidate = sqlite_ledger.prepared_candidate(
        identity.ledger_epoch, CATEGORY, identity.catalog_diff_hash
    )
    assert candidate is not None
    assert candidate.payload["identity"] == {
        "mi_master_sha256": identity.mi_master_sha256,
        "catalog_diff_hash": identity.catalog_diff_hash,
        "run_id": identity.run_id,
    }
    assert "upload_manifest" not in candidate.payload


def test_definition_refresh_stage_contract_reports_only_omitted_stage(
    sqlite_ledger, tmp_path: Path
) -> None:
    # Given: a completed candidate with one durable stage event deliberately missing.
    identity = _identity()
    workspace = _workspace(tmp_path)
    prepare_definition_refresh_candidate(sqlite_ledger, identity, workspace)
    assert sqlite_ledger.mark_publish_running(
        identity.ledger_epoch,
        CATEGORY,
        identity.catalog_diff_hash,
        build_run_id=identity.run_id,
        publish_job_name="mi-master-publish-run-mi-master-1",
        approved_by="pl@example.com",
        approved_at="2026-08-06T00:00:00+00:00",
    )
    run_approved_definition_publish(_request(sqlite_ledger, identity, workspace))
    omitted = "backup_preflight"
    events = [
        event
        for event in sqlite_ledger.stage_events(
            identity.ledger_epoch, CATEGORY, identity.catalog_diff_hash
        )
        if event.stage != omitted
    ]

    # When / Then: the contract names only the omitted stage.
    with pytest.raises(MissingStageError) as excinfo:
        assert_complete_stage_contract(events)
    assert excinfo.value.missing == (omitted,)


def test_approved_publish_enforces_preconditions_refresh_scope_and_invalidation(
    sqlite_ledger, tmp_path: Path
) -> None:
    # Given: an approved MI Master definition candidate.
    identity = _identity()
    workspace = _workspace(tmp_path)
    prepare_definition_refresh_candidate(sqlite_ledger, identity, workspace)
    assert sqlite_ledger.mark_publish_running(
        identity.ledger_epoch,
        CATEGORY,
        identity.catalog_diff_hash,
        build_run_id=identity.run_id,
        publish_job_name="mi-master-publish-run-mi-master-1",
        approved_by="pl@example.com",
        approved_at="2026-08-06T00:00:00+00:00",
    )

    publisher = RecordingPublisher([])
    refresher = RecordingCacheRefresher([])
    invalidator = RecordingInvalidator([])

    # When: the publish is orchestrated.
    assert (
        run_approved_definition_publish(
            _request(
            sqlite_ledger,
            identity,
            workspace,
            publisher=publisher,
            cache_refresher=refresher,
            invalidator=invalidator,
            )
        )
        == 0
    )

    # Then: state advances publish_running -> complete and only lightweight caches refresh.
    assert (
        sqlite_ledger.status(
            identity.ledger_epoch, CATEGORY, identity.catalog_diff_hash
        ).status
        == STATUS_COMPLETE
    )
    transitions = [
        transition.status
        for transition in sqlite_ledger.status_transitions(
            identity.ledger_epoch, CATEGORY, identity.catalog_diff_hash
        )
    ]
    assert transitions[-3:] == [
        STATUS_AWAITING_APPROVAL,
        STATUS_PUBLISH_RUNNING,
        STATUS_COMPLETE,
    ]
    assert refresher.requested == [("cache_brands", "cache_market_status")]
    assert "cache_cause" not in refresher.requested[0]
    assert "cache_deep_analysis" not in refresher.requested[0]
    assert invalidator.identities == [identity]
    assert publisher.calls == [workspace]
    assert_complete_stage_contract(
        sqlite_ledger.stage_events(
            identity.ledger_epoch, CATEGORY, identity.catalog_diff_hash
        )
    )


def test_publish_rejects_extra_cache_refresh_tables(sqlite_ledger, tmp_path: Path) -> None:
    # Given: an approved candidate and a refresher that reports a forbidden cache table.
    identity = _identity()
    workspace = _workspace(tmp_path)
    prepare_definition_refresh_candidate(sqlite_ledger, identity, workspace)
    assert sqlite_ledger.mark_publish_running(
        identity.ledger_epoch,
        CATEGORY,
        identity.catalog_diff_hash,
        build_run_id=identity.run_id,
        publish_job_name="mi-master-publish-run-mi-master-1",
        approved_by="pl@example.com",
        approved_at="2026-08-06T00:00:00+00:00",
    )

    # When / Then: cache_cause/cache_deep_analysis cannot enter this path.
    with pytest.raises(RuntimeError, match="forbidden cache refresh"):
        run_approved_definition_publish(
            _request(
            sqlite_ledger,
            identity,
            workspace,
            publisher=RecordingPublisher([]),
            cache_refresher=RecordingCacheRefresher(
                [], returned=("cache_brands", "cache_cause")
            ),
            invalidator=RecordingInvalidator([]),
            )
        )


def test_publish_fails_before_atomic_interface_when_candidate_precondition_missing(
    sqlite_ledger, tmp_path: Path
) -> None:
    # Given: an approved candidate whose candidate directory disappeared.
    identity = _identity()
    workspace = _workspace(tmp_path)
    prepare_definition_refresh_candidate(sqlite_ledger, identity, workspace)
    workspace.candidate_root.rmdir()
    assert sqlite_ledger.mark_publish_running(
        identity.ledger_epoch,
        CATEGORY,
        identity.catalog_diff_hash,
        build_run_id=identity.run_id,
        publish_job_name="mi-master-publish-run-mi-master-1",
        approved_by="pl@example.com",
        approved_at="2026-08-06T00:00:00+00:00",
    )
    publisher = RecordingPublisher([])

    # When / Then: the atomic publish interface is never called.
    with pytest.raises(RuntimeError, match="candidate_root"):
        run_approved_definition_publish(
            _request(
            sqlite_ledger,
            identity,
            workspace,
            publisher=publisher,
            cache_refresher=RecordingCacheRefresher([]),
            invalidator=RecordingInvalidator([]),
            )
        )
    assert publisher.calls == []
