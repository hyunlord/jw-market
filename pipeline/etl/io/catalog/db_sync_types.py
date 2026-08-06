from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


CATALOG_TABLE_BATCH_LIMIT = 200


@dataclass(frozen=True)
class CatalogColumn:
    name: str
    sql_type: str
    nullable: bool = True


@dataclass(frozen=True)
class CatalogTableSpec:
    parquet_name: str
    table_name: str
    primary_key: str
    columns: tuple[CatalogColumn, ...]


@dataclass(frozen=True)
class CatalogSyncResult:
    table_name: str
    parquet_path: Path
    rows: int
    source_file_versions: tuple[str, ...]
    source_checksum: str
    mi_master_sha256: str | None
    batch_size: int
    dry_run: bool


@dataclass(frozen=True)
class CatalogParityResult:
    parquet_name: str
    table_name: str
    candidate_rows: int
    serving_rows: int
    missing_primary_keys: tuple[str, ...]
    added_primary_keys: tuple[str, ...]
    changed_primary_keys: tuple[str, ...]

    @property
    def matches(self) -> bool:
        return not (
            self.missing_primary_keys
            or self.added_primary_keys
            or self.changed_primary_keys
        )


@dataclass(frozen=True)
class ServingCatalogExport:
    parquet_name: str
    table_name: str
    rows: int
    source_file_versions: tuple[str, ...]
    manifest_hash: str
    mi_master_sha256: str | None


@dataclass(frozen=True)
class CatalogReplacementApproval:
    removed_ids_by_table: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True)
class CatalogReplacementReferenceReport:
    referenced_ids_by_table: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    inactive_decisions_by_table: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    grounded: bool = False


CATALOG_ML_MARKET = CatalogTableSpec(
    parquet_name="ml_market",
    table_name="catalog_ml_market",
    primary_key="ml_id",
    columns=(
        CatalogColumn("ml_id", "VARCHAR(32)", nullable=False),
        CatalogColumn("name", "VARCHAR(255)"),
        CatalogColumn("data_source", "VARCHAR(32)"),
        CatalogColumn("atc_codes_json", "LONGTEXT"),
        CatalogColumn("analyze_class", "TINYINT(1)"),
        CatalogColumn("analyze_molecule", "TINYINT(1)"),
        CatalogColumn("analyze_dosage_form", "TINYINT(1)"),
        CatalogColumn("analyze_strength_pack", "TINYINT(1)"),
        CatalogColumn("analyze_nhi_type", "TINYINT(1)"),
        CatalogColumn("analyze_ox_gx", "TINYINT(1)"),
        CatalogColumn("analyze_fish_oil", "TINYINT(1)"),
        CatalogColumn("target_iqvia_1", "VARCHAR(255)"),
        CatalogColumn("target_iqvia_2", "VARCHAR(255)"),
        CatalogColumn("target_iqvia_3", "VARCHAR(255)"),
        CatalogColumn("target_ubist_1", "VARCHAR(255)"),
        CatalogColumn("target_ubist_2", "VARCHAR(255)"),
        CatalogColumn("target_ubist_3", "VARCHAR(255)"),
        CatalogColumn("target_ubist_4", "VARCHAR(255)"),
        CatalogColumn("source_file_version", "VARCHAR(512)"),
        CatalogColumn("mi_master_sha256", "CHAR(64)"),
        CatalogColumn("ingested_at", "DATETIME(6)"),
        CatalogColumn("catalog_manifest_hash", "CHAR(64)"),
    ),
)

CATALOG_CD_MARKET = CatalogTableSpec(
    parquet_name="cd_market",
    table_name="catalog_cd_market",
    primary_key="cd_id",
    columns=(
        CatalogColumn("cd_id", "VARCHAR(32)", nullable=False),
        CatalogColumn("name", "VARCHAR(255)"),
        CatalogColumn("ml_id", "VARCHAR(32)"),
        CatalogColumn("cd_filter_id", "VARCHAR(32)"),
        CatalogColumn("data_source", "VARCHAR(32)"),
        CatalogColumn("analyze_class", "TINYINT(1)"),
        CatalogColumn("analyze_molecule", "TINYINT(1)"),
        CatalogColumn("analyze_dosage_form", "TINYINT(1)"),
        CatalogColumn("analyze_strength_pack", "TINYINT(1)"),
        CatalogColumn("analyze_nhi_type", "TINYINT(1)"),
        CatalogColumn("analyze_ox_gx", "TINYINT(1)"),
        CatalogColumn("analyze_fish_oil", "TINYINT(1)"),
        CatalogColumn("target_iqvia_1", "VARCHAR(255)"),
        CatalogColumn("target_iqvia_2", "VARCHAR(255)"),
        CatalogColumn("target_iqvia_3", "VARCHAR(255)"),
        CatalogColumn("target_ubist_1", "VARCHAR(255)"),
        CatalogColumn("target_ubist_2", "VARCHAR(255)"),
        CatalogColumn("target_ubist_3", "VARCHAR(255)"),
        CatalogColumn("target_ubist_4", "VARCHAR(255)"),
        CatalogColumn("source_file_version", "VARCHAR(512)"),
        CatalogColumn("mi_master_sha256", "CHAR(64)"),
        CatalogColumn("ingested_at", "DATETIME(6)"),
        CatalogColumn("catalog_manifest_hash", "CHAR(64)"),
    ),
)

CATALOG_STRATEGIC_BRAND = CatalogTableSpec(
    parquet_name="strategic_brand",
    table_name="catalog_strategic_brand",
    primary_key="brand_id",
    columns=(
        CatalogColumn("brand_id", "VARCHAR(128)", nullable=False),
        CatalogColumn("name", "VARCHAR(255)"),
        CatalogColumn("merge_name", "VARCHAR(255)"),
        CatalogColumn("ml_id", "VARCHAR(32)"),
        CatalogColumn("cd_id", "VARCHAR(32)"),
        CatalogColumn("is_excluded", "TINYINT(1)"),
        CatalogColumn("is_class_excluded", "TINYINT(1)"),
        CatalogColumn("allowed_atc4_codes_json", "LONGTEXT"),
        CatalogColumn("class", "VARCHAR(255)"),
        CatalogColumn("class_1", "VARCHAR(255)"),
        CatalogColumn("class_2", "VARCHAR(255)"),
        CatalogColumn("molecule", "VARCHAR(255)"),
        CatalogColumn("dosage_form", "VARCHAR(255)"),
        CatalogColumn("strength_pack", "LONGTEXT"),
        CatalogColumn("nhi_type", "VARCHAR(255)"),
        CatalogColumn("ox_gx", "VARCHAR(255)"),
        CatalogColumn("fish_oil", "VARCHAR(255)"),
        CatalogColumn("판매사", "VARCHAR(255)"),
        CatalogColumn("제조사", "VARCHAR(255)"),
        CatalogColumn("source_file_version", "VARCHAR(512)"),
        CatalogColumn("mi_master_sha256", "CHAR(64)"),
        CatalogColumn("ingested_at", "DATETIME(6)"),
        CatalogColumn("is_jw", "TINYINT(1)"),
        CatalogColumn("is_target", "TINYINT(1)"),
        CatalogColumn("canonical_name", "VARCHAR(255)"),
        CatalogColumn("general_brand_key", "VARCHAR(255)"),
        CatalogColumn("strategy_id", "VARCHAR(32)"),
        CatalogColumn("catalog_manifest_hash", "CHAR(64)"),
    ),
)

CATALOG_TABLES = (CATALOG_ML_MARKET, CATALOG_CD_MARKET, CATALOG_STRATEGIC_BRAND)
