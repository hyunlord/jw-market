"""Schema objects and DDL for the brand-molecule bridge.

``mart_brand_molecule`` is additive to existing marts.  It does not replace
general/strategic mart tables; it only gives the dynamic API a fast indexed
path from normalized molecule filters to brand keys.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, TypedDict


MART_BRAND_MOLECULE_DDL: Final[str] = """
CREATE TABLE IF NOT EXISTS mart_brand_molecule (
  id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  brand_key VARCHAR(255) NOT NULL,
  brand_name VARCHAR(255) NOT NULL,
  atc4_code VARCHAR(16) NOT NULL DEFAULT '',
  mart_source VARCHAR(16) NOT NULL DEFAULT 'any',
  molecule_norm VARCHAR(255) NOT NULL,
  molecule_display VARCHAR(255) NOT NULL,
  molecule_raw_examples JSON NOT NULL,
  evidence_scopes JSON NOT NULL,
  evidence_count INT NOT NULL,
  component_count INT NOT NULL,
  is_combo_component TINYINT(1) NOT NULL DEFAULT 0,
  computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_brand_molecule (brand_key, atc4_code, mart_source, molecule_norm),
  INDEX idx_molecule_norm (molecule_norm, mart_source, atc4_code, brand_key),
  INDEX idx_brand_key (brand_key, mart_source, atc4_code),
  INDEX idx_atc4_source (atc4_code, mart_source)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""

BRIDGE_INSERT_COLUMNS: Final[tuple[str, ...]] = (
    "brand_key",
    "brand_name",
    "atc4_code",
    "mart_source",
    "molecule_norm",
    "molecule_display",
    "molecule_raw_examples",
    "evidence_scopes",
    "evidence_count",
    "component_count",
    "is_combo_component",
)


@dataclass(frozen=True, slots=True)
class MoleculeBridgeRecord:
    """One evidence row before source-level consolidation."""

    brand_key: str
    brand_name: str
    atc4_code: str
    mart_source: str
    molecule_norm: str
    molecule_display: str
    molecule_raw: str
    evidence_scope: str
    component_count: int
    is_combo_component: bool


@dataclass(frozen=True, slots=True)
class BridgeBuildStats:
    """Build summary printed by the ETL stage and copied into audit logs."""

    target_db: str
    source_db: str
    inserted_rows: int
    candidate_rows: int
    brand_keys: int
    molecule_norms: int
    combo_rows: int


class BridgeInsertPayload(TypedDict):
    """Typed shape passed to PyMySQL ``executemany``."""

    brand_key: str
    brand_name: str
    atc4_code: str
    mart_source: str
    molecule_norm: str
    molecule_display: str
    molecule_raw_examples: str
    evidence_scopes: str
    evidence_count: int
    component_count: int
    is_combo_component: int


def bridge_record_key(record: MoleculeBridgeRecord) -> tuple[str, str, str, str]:
    """Return the table uniqueness key for one bridge record."""

    return (record.brand_key, record.atc4_code, record.mart_source, record.molecule_norm)
