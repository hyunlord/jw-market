"""MI Master definition-refresh orchestration interfaces."""
from __future__ import annotations

from typing import Protocol

from pipeline.scripts.ingest_hook.mi_master_definition_contract import (
    DefinitionRefreshIdentity,
    DefinitionRefreshRequest,
    PublishReceipt,
    PublishWorkspace,
)


class PrepareAdapters(Protocol):
    def catalog_sync(self, request: DefinitionRefreshRequest) -> None: ...
    def scope_plan(self, request: DefinitionRefreshRequest) -> None: ...
    def candidate_build(self, request: DefinitionRefreshRequest) -> None: ...
    def sigma(self, request: DefinitionRefreshRequest) -> None: ...
    def post_gate(self, request: DefinitionRefreshRequest) -> None: ...


class AtomicPublishOrchestrator(Protocol):
    def publish(self, workspace: PublishWorkspace, identity: DefinitionRefreshIdentity) -> PublishReceipt: ...


class CacheRefresher(Protocol):
    def refresh_tables(self, tables: tuple[str, ...]) -> tuple[str, ...]: ...


class RuntimeCatalogInvalidator(Protocol):
    def invalidate(self, identity: DefinitionRefreshIdentity) -> None: ...
