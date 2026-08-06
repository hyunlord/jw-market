"""Typed contracts for MI Master definition refresh gates."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping


SUPPORTED_REFRESH_CACHE_TABLES = ("cache_brands", "cache_market_status")
LIVE_CATALOG_TABLES = (
    "catalog_ml_market",
    "catalog_cd_market",
    "catalog_strategic_brand",
)
STRATEGIC_REFRESH_TABLES = (
    "mart_strategic_ml_brand_metric",
    "mart_strategic_ml_market_metric",
    "mart_strategic_cd_brand_metric",
    "mart_strategic_cd_market_metric",
)


class ReplacementReferencePolicy:
    APPEND_ONLY = "append_only"
    APPEND_OR_APPROVED_REMOVAL = "append_or_approved_removal"


@dataclass(frozen=True, slots=True)
class MiMasterRefreshCandidate:
    candidate_id: str
    mi_master_sha256: str
    manifest_sha256: str
    allowed_cache_tables: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DefinitionApprovalIdentity:
    mi_master_sha256: str
    catalog_diff_hash: str
    run_id: str

    def as_dict(self) -> dict[str, str]:
        return {
            "mi_master_sha256": self.mi_master_sha256,
            "catalog_diff_hash": self.catalog_diff_hash,
            "run_id": self.run_id,
        }


@dataclass(frozen=True, slots=True)
class RemovedIdApproval:
    approved: bool
    removed_ids: tuple[str, ...]
    approver: str
    reason: str


@dataclass(frozen=True, slots=True)
class ReplacementDiff:
    removed_ids: tuple[str, ...]
    added_ids: tuple[str, ...]
    unchanged_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReplacementTableParity:
    table_name: str
    row_count_before: int
    row_count_after: int
    row_count_expected: int
    removed_ids: tuple[str, ...]
    added_ids: tuple[str, ...]
    changed_ids: tuple[str, ...]
    before_parquet_sha256: str
    after_parquet_sha256: str


@dataclass(frozen=True, slots=True)
class AffectedDefinition:
    market_id: str
    atc4_codes: tuple[str, ...]
    cache_tables: tuple[str, ...]
    cd_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AffectedScopePlan:
    market_ids: tuple[str, ...]
    cache_tables: tuple[str, ...]
    general_rebuild_atc4: tuple[str, ...]
    affected_ml_ids: tuple[str, ...] = ()
    affected_cd_ids: tuple[str, ...] = ()
    unchanged_ml_ids: tuple[str, ...] = ()
    unchanged_cd_ids: tuple[str, ...] = ()

    @property
    def general_rebuild_count(self) -> int:
        return len(self.general_rebuild_atc4)


@dataclass(frozen=True, slots=True)
class RefreshCorpus:
    candidate_dir: Path
    backup_dir: Path


@dataclass(frozen=True, slots=True)
class RefreshPublishPlan:
    candidate: MiMasterRefreshCandidate
    candidate_dir: Path
    live_dir: Path
    backup_dir: Path
    journal_path: Path
    corpus: RefreshCorpus | None = None
    approval_identity: DefinitionApprovalIdentity | None = None


@dataclass(frozen=True, slots=True)
class RefreshPublishResult:
    live_dir: Path
    backup_dir: Path
    journal_path: Path


@dataclass(frozen=True, slots=True)
class CatalogTableSnapshot:
    table_name: str
    row_count: int
    sha256: str


@dataclass(frozen=True, slots=True)
class CandidateSeedContract:
    live_catalog: tuple[CatalogTableSnapshot, ...]
    strategic_tables: tuple[CatalogTableSnapshot, ...]


@dataclass(frozen=True, slots=True)
class ReferenceReport:
    mart_references: Mapping[str, tuple[str, ...]]
    cache_references: Mapping[str, tuple[str, ...]]
    saved_filter_references: Mapping[str, tuple[str, ...]]
    inactive_decisions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StrategicMarketValidationInput:
    unchanged_market_hash_before: Mapping[str, str]
    unchanged_market_hash_after: Mapping[str, str]
    ml_members: Mapping[str, tuple[str, ...]]
    cd_members: Mapping[str, tuple[str, ...]]
    cd_parent_ml: Mapping[str, str]
    sigma_before: Mapping[str, int]
    sigma_after: Mapping[str, int]

    def with_overrides(
        self,
        **changes: Mapping[str, str] | Mapping[str, int] | Mapping[str, tuple[str, ...]],
    ) -> "StrategicMarketValidationInput":
        return replace(self, **changes)
