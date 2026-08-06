"""Backend contract conversion helpers for MI definition refresh."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from pipeline.etl.mi_master_refresh.contracts import (
    AffectedDefinition,
    CandidateSeedContract,
    CatalogTableSnapshot,
    DefinitionApprovalIdentity,
    MiMasterRefreshCandidate,
    ReferenceReport,
    RefreshCorpus,
    RefreshPublishPlan,
    RemovedIdApproval,
    StrategicMarketValidationInput,
)


def affected(payload: Mapping[str, object]) -> AffectedDefinition:
    return AffectedDefinition(
        market_id=str(payload.get("market_id") or ""),
        atc4_codes=str_tuple(payload, "atc4_codes"),
        cache_tables=str_tuple(payload, "cache_tables"),
        cd_ids=str_tuple(payload, "cd_ids"),
    )


def validation_input(payload: Mapping[str, object]) -> StrategicMarketValidationInput:
    return StrategicMarketValidationInput(
        unchanged_market_hash_before=str_map(payload, "unchanged_market_hash_before"),
        unchanged_market_hash_after=str_map(payload, "unchanged_market_hash_after"),
        ml_members=tuple_map(payload, "ml_members"),
        cd_members=tuple_map(payload, "cd_members"),
        cd_parent_ml=str_map(payload, "cd_parent_ml"),
        sigma_before=int_map(payload, "sigma_before"),
        sigma_after=int_map(payload, "sigma_after"),
    )


def publish_plan(payload: Mapping[str, object]) -> RefreshPublishPlan:
    return RefreshPublishPlan(
        candidate=candidate(mapping(payload, "candidate")),
        candidate_dir=path_value(payload, "candidate_dir"),
        live_dir=path_value(payload, "live_dir"),
        backup_dir=path_value(payload, "backup_dir"),
        journal_path=path_value(payload, "journal_path"),
        corpus=corpus(mapping(payload, "corpus")),
        approval_identity=approval_identity(mapping(payload, "approval_identity")),
    )


def candidate(payload: Mapping[str, object]) -> MiMasterRefreshCandidate:
    return MiMasterRefreshCandidate(
        candidate_id=str(payload.get("candidate_id") or ""),
        mi_master_sha256=str(payload.get("mi_master_sha256") or ""),
        manifest_sha256=str(payload.get("manifest_sha256") or ""),
        allowed_cache_tables=str_tuple(payload, "allowed_cache_tables"),
    )


def approval_identity(payload: Mapping[str, object]) -> DefinitionApprovalIdentity:
    return DefinitionApprovalIdentity(
        mi_master_sha256=str(payload.get("mi_master_sha256") or ""),
        catalog_diff_hash=str(payload.get("catalog_diff_hash") or ""),
        run_id=str(payload.get("run_id") or ""),
    )


def corpus(payload: Mapping[str, object]) -> RefreshCorpus:
    return RefreshCorpus(
        candidate_dir=path_value(payload, "candidate_dir"),
        backup_dir=path_value(payload, "backup_dir"),
    )


def seed_contract(payload: Mapping[str, object]) -> CandidateSeedContract:
    return CandidateSeedContract(
        live_catalog=tuple(snapshot(item) for item in dict_list(payload, "live_catalog")),
        strategic_tables=tuple(snapshot(item) for item in dict_list(payload, "strategic_tables")),
    )


def snapshot(payload: Mapping[str, object]) -> CatalogTableSnapshot:
    return CatalogTableSnapshot(
        table_name=str(payload.get("table_name") or ""),
        row_count=int(payload.get("row_count") or 0),
        sha256=str(payload.get("sha256") or ""),
    )


def removed_approval(payload: Mapping[str, object]) -> RemovedIdApproval:
    return RemovedIdApproval(
        approved=bool(payload.get("approved")),
        removed_ids=str_tuple(payload, "removed_ids"),
        approver=str(payload.get("approver") or ""),
        reason=str(payload.get("reason") or ""),
    )


def reference_report(payload: Mapping[str, object]) -> ReferenceReport:
    return ReferenceReport(
        mart_references=tuple_map(payload, "mart_references"),
        cache_references=tuple_map(payload, "cache_references"),
        saved_filter_references=tuple_map(payload, "saved_filter_references"),
        inactive_decisions=str_tuple(payload, "inactive_decisions"),
    )


def mapping(payload: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise RuntimeError(f"definition request {key} must be an object")
    return value


def dict_list(payload: Mapping[str, object], key: str) -> Sequence[Mapping[str, object]]:
    value = payload.get(key)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise RuntimeError(f"definition request {key} must be an object list")
    return value


def str_tuple(payload: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = payload.get(key, ())
    if not isinstance(value, list):
        raise RuntimeError(f"definition request {key} must be a list")
    return tuple(str(item) for item in value)


def str_map(payload: Mapping[str, object], key: str) -> Mapping[str, str]:
    value = mapping(payload, key)
    return {str(item_key): str(item_value) for item_key, item_value in value.items()}


def int_map(payload: Mapping[str, object], key: str) -> Mapping[str, int]:
    value = mapping(payload, key)
    return {str(item_key): int(item_value) for item_key, item_value in value.items()}


def tuple_map(payload: Mapping[str, object], key: str) -> Mapping[str, tuple[str, ...]]:
    value = mapping(payload, key)
    return {
        str(item_key): tuple(str(item) for item in item_value)
        for item_key, item_value in value.items()
        if isinstance(item_value, list)
    }


def path_value(payload: Mapping[str, object], key: str) -> Path:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise RuntimeError(f"definition request {key} is required")
    return Path(value)
