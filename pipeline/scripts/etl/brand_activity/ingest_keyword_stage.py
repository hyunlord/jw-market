"""Keyword stage DDL helpers for the brand-activity isolated loader."""

from __future__ import annotations

import re
from typing import Final


DEFAULT_STAGE_SCHEMA: Final[str] = "jw_brand_activity_stage"
KEYWORD_TABLE: Final[str] = "km_keyword_event_stage"
BRAND_ACTIVITY_SCHEMA_PATTERN: Final = re.compile(
    r"^(?:jw_brand_activity_|jw_ingest_)[A-Za-z0-9_]+$"
)


def quote_schema_name(schema: str) -> str:
    """Validate that a DB schema name is safe and isolated to stage use."""
    if re.fullmatch(r"[A-Za-z0-9_]+", schema) is None:
        raise ValueError(f"unsafe schema name: {schema!r}")
    if schema != DEFAULT_STAGE_SCHEMA and BRAND_ACTIVITY_SCHEMA_PATTERN.fullmatch(schema) is None:
        raise ValueError(f"refusing schema outside {DEFAULT_STAGE_SCHEMA} or brand-activity scratch schema: {schema!r}")
    return schema


def stage_ddl(schema: str) -> str:
    """Build the append-preserving Keyword stage table DDL statement."""
    safe_schema = quote_schema_name(schema)
    return f"""CREATE SCHEMA IF NOT EXISTS `{safe_schema}`;

CREATE TABLE IF NOT EXISTS `{safe_schema}`.`{KEYWORD_TABLE}` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `period_ym` char(7) NOT NULL,
  `visit_location` varchar(255) NOT NULL,
  `specialty` varchar(255) NOT NULL,
  `representing_company` varchar(255) NOT NULL,
  `product_name` varchar(255) NOT NULL,
  `therapeutic_class` varchar(64) NOT NULL,
  `keyword_text` longtext NOT NULL,
  `interest` varchar(64) NOT NULL,
  `prescription_frequency` varchar(128) NOT NULL,
  `prescription_evolution` varchar(128) NOT NULL,
  `abstract_lit` varchar(16) NOT NULL,
  `patient_lit` varchar(16) NOT NULL,
  `promotional_lit` varchar(16) NOT NULL,
  `samples_left` varchar(16) NOT NULL,
  `other_materials_left` varchar(16) NOT NULL,
  `what_other_materials` text NOT NULL,
  `other_comments` text NOT NULL,
  `source_file` varchar(255) NOT NULL,
  `source_sheet` varchar(64) NOT NULL,
  `source_row_no` int NOT NULL,
  `source_file_sha256` char(64) NOT NULL,
  `stage_row_sha256` char(64) NOT NULL,
  `loaded_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_km_keyword_period_class` (`period_ym`, `therapeutic_class`),
  KEY `idx_km_keyword_product` (`product_name`),
  KEY `idx_km_keyword_lineage` (`source_file`, `source_sheet`, `source_row_no`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""
