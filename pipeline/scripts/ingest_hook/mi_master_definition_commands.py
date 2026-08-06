"""Typed adapters that invoke checked-in MI Master refresh functions."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from pipeline.etl.stages import s2_catalog
from pipeline.scripts.ingest_hook.mi_master_definition_contract import (
    DefinitionRefreshIdentity,
    DefinitionRefreshRequest,
    PipelineCatalogSync,
    PublishReceipt,
    PublishWorkspace,
)

StageFunction = Callable[[DefinitionRefreshRequest], None]
PublishFunction = Callable[[PublishWorkspace, DefinitionRefreshIdentity], PublishReceipt]
CacheRefreshFunction = Callable[[tuple[str, ...]], tuple[str, ...]]
InvalidateFunction = Callable[[DefinitionRefreshIdentity], None]


def _not_implemented(stage: str) -> None:
    raise RuntimeError(f"NOT_IMPLEMENTED: {stage} real adapter is not bound")


def _s2_params(config: PipelineCatalogSync) -> dict[str, object]:
    return {
        "target_dir": config.output_root,
        "input_file": config.input_file,
        "catalog_root": config.catalog_root,
        "cache_dir": config.cache_dir,
        "inputs_dir": config.inputs_dir,
        "ubist_dir": config.ubist_dir,
        "iqvia_nsa_dir": config.iqvia_nsa_dir,
        "ingested_at": config.ingested_at,
    }


def run_s2_catalog_sync(request: DefinitionRefreshRequest) -> None:
    config = request.catalog_sync
    if config is None:
        _not_implemented("catalog_sync")
    rc = s2_catalog.run(_s2_params(config))
    if rc != 0:
        raise RuntimeError(f"s2 catalog sync failed rc={rc}")
    if not config.catalog_root.exists():
        raise RuntimeError(f"s2 catalog sync produced no catalog root: {config.catalog_root}")


@dataclass(frozen=True, slots=True)
class PrepareFunctionBindings:
    catalog_sync: StageFunction | None = None
    scope_plan: StageFunction | None = None
    candidate_build: StageFunction | None = None
    sigma: StageFunction | None = None
    post_gate: StageFunction | None = None


@dataclass(frozen=True, slots=True)
class PipelinePrepareAdapters:
    bindings: PrepareFunctionBindings

    def _call(self, stage: str, fn: StageFunction | None, request: DefinitionRefreshRequest) -> None:
        if fn is None:
            _not_implemented(stage)
        fn(request)

    def catalog_sync(self, request: DefinitionRefreshRequest) -> None:
        self._call("catalog_sync", self.bindings.catalog_sync, request)

    def scope_plan(self, request: DefinitionRefreshRequest) -> None:
        self._call("scope_plan", self.bindings.scope_plan, request)

    def candidate_build(self, request: DefinitionRefreshRequest) -> None:
        self._call("candidate_build", self.bindings.candidate_build, request)

    def sigma(self, request: DefinitionRefreshRequest) -> None:
        self._call("sigma", self.bindings.sigma, request)

    def post_gate(self, request: DefinitionRefreshRequest) -> None:
        self._call("post_gate", self.bindings.post_gate, request)


def prepare_adapters_from_request(request: DefinitionRefreshRequest) -> PipelinePrepareAdapters:
    return PipelinePrepareAdapters(
        PrepareFunctionBindings(
            catalog_sync=run_s2_catalog_sync if request.catalog_sync is not None else None
        )
    )


@dataclass(frozen=True, slots=True)
class PipelinePublisher:
    publish_fn: PublishFunction | None = None

    def publish(
        self, workspace: PublishWorkspace, identity: DefinitionRefreshIdentity
    ) -> PublishReceipt:
        if self.publish_fn is None:
            _not_implemented("mart_publish")
        return self.publish_fn(workspace, identity)


@dataclass(frozen=True, slots=True)
class PipelineCacheRefresher:
    refresh_fn: CacheRefreshFunction | None = None

    def refresh_tables(self, tables: tuple[str, ...]) -> tuple[str, ...]:
        if self.refresh_fn is None:
            _not_implemented("cache_refresh")
        return self.refresh_fn(tables)


@dataclass(frozen=True, slots=True)
class PipelineRuntimeCatalogInvalidator:
    invalidate_fn: InvalidateFunction | None = None

    def invalidate(self, identity: DefinitionRefreshIdentity) -> None:
        if self.invalidate_fn is None:
            _not_implemented("catalog_invalidate")
        self.invalidate_fn(identity)
