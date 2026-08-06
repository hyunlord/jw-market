from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pipeline.scripts.ingest_hook.ledger import Ledger
from pipeline.scripts.ingest_hook.mi_master_definition_refresh import (
    ALLOWED_CACHE_REFRESH_TABLES,
    CATEGORY,
    DefinitionPublishAdapters,
    DefinitionPublishRequest,
)
from pipeline.scripts.ingest_hook.mi_master_definition_contract import (
    DefinitionRefreshIdentity,
    DefinitionRefreshRequest,
    PublishReceipt,
    PublishWorkspace,
)


@dataclass
class RecordingPrepareAdapters:
    calls: list[str]
    fail_at: str | None = None

    def _record(self, stage: str) -> None:
        self.calls.append(stage)
        if self.fail_at == stage:
            raise RuntimeError(f"injected {stage} failure")

    def catalog_sync(self, request: DefinitionRefreshRequest) -> None:
        self._record("catalog_sync")

    def scope_plan(self, request: DefinitionRefreshRequest) -> None:
        self._record("scope_plan")

    def candidate_build(self, request: DefinitionRefreshRequest) -> None:
        self._record("candidate_build")

    def sigma(self, request: DefinitionRefreshRequest) -> None:
        self._record("sigma")

    def post_gate(self, request: DefinitionRefreshRequest) -> None:
        self._record("post_gate")


@dataclass
class RecordingPublisher:
    calls: list[PublishWorkspace]
    fail: bool = False

    def publish(
        self, workspace: PublishWorkspace, identity: DefinitionRefreshIdentity
    ) -> PublishReceipt:
        self.calls.append(workspace)
        if self.fail:
            raise RuntimeError("injected mart_publish failure")
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
    preflight_identities: list[DefinitionRefreshIdentity] | None = None

    def preflight(self, identity: DefinitionRefreshIdentity) -> None:
        if self.preflight_identities is not None:
            self.preflight_identities.append(identity)

    def invalidate(self, identity: DefinitionRefreshIdentity) -> None:
        self.identities.append(identity)


def identity() -> DefinitionRefreshIdentity:
    return DefinitionRefreshIdentity(
        mi_master_sha256="a" * 64,
        catalog_diff_hash="b" * 64,
        run_id="run-mi-master-1",
    )


def workspace(tmp_path: Path) -> PublishWorkspace:
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


def prepare_request(tmp_path: Path) -> DefinitionRefreshRequest:
    return DefinitionRefreshRequest(identity=identity(), workspace=workspace(tmp_path))


def approve(ledger: Ledger, request: DefinitionRefreshRequest) -> None:
    assert ledger.mark_publish_running(
        request.identity.ledger_epoch,
        CATEGORY,
        request.identity.catalog_diff_hash,
        build_run_id=request.identity.run_id,
        publish_job_name="mi-master-publish-run-mi-master-1",
        approved_by="pl@example.com",
        approved_at="2026-08-06T00:00:00+00:00",
    )


def publish_request(
    ledger: Ledger,
    request: DefinitionRefreshRequest,
    *,
    publisher: RecordingPublisher | None = None,
    cache_refresher: RecordingCacheRefresher | None = None,
    invalidator: RecordingInvalidator | None = None,
) -> DefinitionPublishRequest:
    return DefinitionPublishRequest(
        ledger=ledger,
        identity=request.identity,
        workspace=request.workspace,
        market_ordinal=request.market_ordinal,
        adapters=DefinitionPublishAdapters(
            publisher=publisher or RecordingPublisher([]),
            cache_refresher=cache_refresher or RecordingCacheRefresher([]),
            invalidator=invalidator or RecordingInvalidator([]),
        ),
    )
