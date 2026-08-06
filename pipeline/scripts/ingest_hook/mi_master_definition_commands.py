"""Typed adapters that invoke checked-in MI Master refresh functions."""
from __future__ import annotations

import json
import sys
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass

from pipeline.etl.mi_master_refresh.provenance import validate_candidate_seed_contract
from pipeline.etl.mi_master_refresh.publication import atomic_publish_candidate
from pipeline.etl.mi_master_refresh.replacement import (
    build_replacement_diff,
    validate_removed_id_references,
    validate_replacement_diff,
)
from pipeline.etl.mi_master_refresh.scope_validation import (
    plan_affected_scope,
    validate_strategic_market_refresh,
)
from pipeline.etl.stages import s2_catalog, s5_mart
from pipeline.scripts.ingest_hook import mi_master_definition_backend as backend
from pipeline.scripts.ingest_hook.mi_master_definition_contract import (
    ALLOWED_CACHE_REFRESH_TABLES,
    CandidateBuildRequest,
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
        "target_db": config.target_db,
        "sync_catalog_db": config.sync_catalog_db,
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


def run_scope_plan(request: DefinitionRefreshRequest) -> None:
    config = request.scope_plan
    if config is None:
        _not_implemented("scope_plan")
    plan = plan_affected_scope(
        affected_definitions=[backend.affected(item) for item in config.affected_definitions],
        existing_general_atc4=config.existing_general_atc4,
        all_ml_ids=config.all_ml_ids,
        all_cd_ids=config.all_cd_ids,
    )
    config.output_path.parent.mkdir(parents=True, exist_ok=True)
    config.output_path.write_text(json.dumps(asdict(plan), sort_keys=True), encoding="utf-8")


def run_candidate_build(request: DefinitionRefreshRequest) -> None:
    config = request.candidate_build
    if config is None:
        _not_implemented("candidate_build")
    rc = s5_mart.run(_s5_params(config))
    if rc != 0:
        raise RuntimeError(f"scoped S5 candidate build failed rc={rc}")


def run_sigma(request: DefinitionRefreshRequest) -> None:
    if request.validation is None:
        _not_implemented("sigma")
    validate_strategic_market_refresh(backend.validation_input(request.validation))


def run_post_gate(request: DefinitionRefreshRequest) -> None:
    payload = request.post_gate
    if payload is None:
        _not_implemented("post_gate")
    seed_contract = backend.seed_contract(backend.mapping(payload, "seed_contract"))
    validate_candidate_seed_contract(seed_contract)
    replacement = payload.get("replacement")
    if isinstance(replacement, dict):
        _validate_replacement(replacement)


def publisher_from_request(request: DefinitionRefreshRequest) -> "PipelinePublisher":
    if request.publish_plan is None:
        return PipelinePublisher()
    return PipelinePublisher(lambda _workspace, _identity: _publish(request.publish_plan))


def cache_refresher_from_request(request: DefinitionRefreshRequest) -> "PipelineCacheRefresher":
    return PipelineCacheRefresher.from_request(request)


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
            catalog_sync=run_s2_catalog_sync if request.catalog_sync is not None else None,
            scope_plan=run_scope_plan if request.scope_plan is not None else None,
            candidate_build=run_candidate_build if request.candidate_build is not None else None,
            sigma=run_sigma if request.validation is not None else None,
            post_gate=run_post_gate if request.post_gate is not None else None,
        )
    )


@dataclass(frozen=True, slots=True)
class PipelinePublisher:
    publish_fn: PublishFunction | None = None

    def publish(self, workspace: PublishWorkspace, identity: DefinitionRefreshIdentity) -> PublishReceipt:
        if self.publish_fn is None:
            _not_implemented("mart_publish")
        return self.publish_fn(workspace, identity)


@dataclass(frozen=True, slots=True)
class PipelineCacheRefresher:
    refresh_fn: CacheRefreshFunction | None = None
    requested_tables: tuple[str, ...] = ALLOWED_CACHE_REFRESH_TABLES

    @classmethod
    def from_request(cls, request: DefinitionRefreshRequest) -> "PipelineCacheRefresher":
        if request.cache_refresh is None:
            return cls()
        tables = request.cache_refresh.get("target_tables", list(ALLOWED_CACHE_REFRESH_TABLES))
        if not isinstance(tables, list):
            raise RuntimeError("cache_refresh target_tables must be a list")
        return cls(_refresh_allowed_cache_tables, tuple(str(table) for table in tables))

    def refresh_tables(self, tables: tuple[str, ...]) -> tuple[str, ...]:
        if self.refresh_fn is None:
            _not_implemented("cache_refresh")
        if tuple(tables) != ALLOWED_CACHE_REFRESH_TABLES or self.requested_tables != ALLOWED_CACHE_REFRESH_TABLES:
            raise RuntimeError(f"forbidden cache refresh table: {self.requested_tables}")
        return self.refresh_fn(tables)


@dataclass(frozen=True, slots=True)
class PipelineRuntimeCatalogInvalidator:
    invalidate_fn: InvalidateFunction | None = None

    @classmethod
    def from_request(cls, _request: DefinitionRefreshRequest) -> "PipelineRuntimeCatalogInvalidator":
        return cls()

    def preflight(self, identity: DefinitionRefreshIdentity) -> None:
        if self.invalidate_fn is None:
            _not_implemented("catalog_invalidate")

    def invalidate(self, identity: DefinitionRefreshIdentity) -> None:
        if self.invalidate_fn is None:
            _not_implemented("catalog_invalidate")
        self.invalidate_fn(identity)


def _s5_params(config: CandidateBuildRequest) -> dict[str, object]:
    return {
        "target_db": config.target_db,
        "source_db": config.source_db,
        "general_source_db": config.general_source_db,
        "catalog_root": config.catalog_root,
        "affected_ml_ids": config.affected_ml_ids,
        "affected_cd_ids": config.affected_cd_ids,
    }


def _validate_replacement(payload: Mapping[str, object]) -> None:
    diff = build_replacement_diff(
        reference_ids=backend.str_tuple(payload, "reference_ids"),
        candidate_ids=backend.str_tuple(payload, "candidate_ids"),
    )
    approval_payload = payload.get("removed_id_approval")
    approval = backend.removed_approval(approval_payload) if isinstance(approval_payload, dict) else None
    validate_replacement_diff(
        diff,
        policy=str(payload.get("policy") or "append_only"),
        removed_id_approval=approval,
    )
    report = backend.reference_report(backend.mapping(payload, "reference_report"))
    validate_removed_id_references(diff.removed_ids, report)


def _publish(payload: Mapping[str, object]) -> PublishReceipt:
    result = atomic_publish_candidate(backend.publish_plan(payload))
    return PublishReceipt(result.journal_path, result.backup_dir)


def _refresh_allowed_cache_tables(tables: tuple[str, ...]) -> tuple[str, ...]:
    from pipeline.scripts.etl import build_cache_brands, build_cache_market_status

    builders = {
        "cache_brands": build_cache_brands.main,
        "cache_market_status": build_cache_market_status.main,
    }
    for table in tables:
        _run_builder(builders[table], table)
    return tables


def _run_builder(main: Callable[[], None], table: str) -> None:
    original = sys.argv[:]
    try:
        sys.argv = [main.__module__, "--target-table", table]
        main()
    finally:
        sys.argv = original
