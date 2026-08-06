"""Compatibility facade for MI Master definition refresh gates."""

from __future__ import annotations

from pipeline.etl.mi_master_refresh.contracts import (
    LIVE_CATALOG_TABLES,
    STRATEGIC_REFRESH_TABLES,
    SUPPORTED_REFRESH_CACHE_TABLES,
    AffectedDefinition,
    AffectedScopePlan,
    CandidateSeedContract,
    CatalogTableSnapshot,
    DefinitionApprovalIdentity,
    MiMasterRefreshCandidate,
    ReferenceReport,
    RefreshCorpus,
    RefreshPublishPlan,
    RefreshPublishResult,
    RemovedIdApproval,
    ReplacementDiff,
    ReplacementReferencePolicy,
    ReplacementTableParity,
    StrategicMarketValidationInput,
)
from pipeline.etl.mi_master_refresh.provenance import (
    load_candidate_seed,
    mi_master_sha256,
    validate_candidate_seed,
    validate_candidate_seed_contract,
    validate_definition_approval,
    validate_manifest_equality,
)
from pipeline.etl.mi_master_refresh.publication import (
    atomic_publish_candidate,
    validate_refresh_publish_plan,
)
from pipeline.etl.mi_master_refresh.replacement import (
    build_catalog_diff_hash,
    build_replacement_diff,
    build_replacement_parity,
    validate_removed_id_references,
    validate_replacement_diff,
    validate_replacement_parity,
)
from pipeline.etl.mi_master_refresh.scope_validation import (
    plan_affected_scope,
    validate_strategic_market_refresh,
)

__all__ = [
    "LIVE_CATALOG_TABLES",
    "STRATEGIC_REFRESH_TABLES",
    "SUPPORTED_REFRESH_CACHE_TABLES",
    "AffectedDefinition",
    "AffectedScopePlan",
    "CandidateSeedContract",
    "CatalogTableSnapshot",
    "DefinitionApprovalIdentity",
    "MiMasterRefreshCandidate",
    "ReferenceReport",
    "RefreshCorpus",
    "RefreshPublishPlan",
    "RefreshPublishResult",
    "RemovedIdApproval",
    "ReplacementDiff",
    "ReplacementReferencePolicy",
    "ReplacementTableParity",
    "StrategicMarketValidationInput",
    "atomic_publish_candidate",
    "build_catalog_diff_hash",
    "build_replacement_diff",
    "build_replacement_parity",
    "load_candidate_seed",
    "mi_master_sha256",
    "plan_affected_scope",
    "validate_candidate_seed",
    "validate_candidate_seed_contract",
    "validate_definition_approval",
    "validate_manifest_equality",
    "validate_refresh_publish_plan",
    "validate_removed_id_references",
    "validate_replacement_diff",
    "validate_replacement_parity",
    "validate_strategic_market_refresh",
]
