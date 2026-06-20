"""Raw staging DDL for brand-activity source rows."""

from __future__ import annotations


RAW_DDL = """
CREATE SCHEMA IF NOT EXISTS `{raw_schema}`;

CREATE TABLE IF NOT EXISTS `{raw_schema}`.`raw_csd_channel_dynamics` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `source_dataset` varchar(32) NOT NULL,
  `source_file` varchar(255) NOT NULL,
  `source_file_sha256` char(64) NOT NULL,
  `source_sheet` varchar(128) NOT NULL,
  `source_row_no` int NOT NULL,
  `source_period_ym` char(7) NOT NULL,
  `period_ym` char(7) NOT NULL,
  `market` varchar(128) NOT NULL,
  `jw_channel` varchar(32) NOT NULL,
  `region` varchar(128) NOT NULL,
  `master_product` varchar(255) NOT NULL,
  `manufacturer` varchar(255) NOT NULL,
  `representing_company` varchar(255) NOT NULL,
  `metric` varchar(64) NOT NULL,
  `product_details` int NOT NULL,
  `selected_for_stage` tinyint(1) NOT NULL,
  `dedup_key` char(64) NOT NULL,
  `source_row_key` char(64) NOT NULL,
  `row_hash` char(64) NOT NULL,
  `raw_payload_json` longtext NOT NULL,
  `loaded_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_raw_csd_source_row` (`source_row_key`),
  KEY `idx_raw_csd_dedup` (`dedup_key`),
  KEY `idx_raw_csd_period_market` (`period_ym`, `market`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `{raw_schema}`.`raw_keyword_events` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `source_dataset` varchar(32) NOT NULL,
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
  `dedup_key` char(64) NOT NULL,
  `row_hash` char(64) NOT NULL,
  `raw_payload_json` longtext NOT NULL,
  `loaded_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_raw_keyword_event` (`dedup_key`),
  KEY `idx_raw_keyword_period_class` (`period_ym`, `therapeutic_class`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `{raw_schema}`.`raw_meeting_events` (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT,
  `source_dataset` varchar(32) NOT NULL,
  `meeting_date` date NOT NULL,
  `period_ym` char(7) NOT NULL,
  `meeting_topic` text NOT NULL,
  `meeting_format` varchar(128) NOT NULL,
  `pharma_sponsor` varchar(255) NOT NULL,
  `non_pharma_sponsor` varchar(255) NOT NULL,
  `no_at_meeting` int NULL,
  `product_name` varchar(255) NOT NULL,
  `therapeutic_class` varchar(64) NOT NULL,
  `prescription_frequency` varchar(128) NOT NULL,
  `prescription_evolution` varchar(128) NOT NULL,
  `interest` varchar(64) NOT NULL,
  `verbatim_message` text NOT NULL,
  `other_comments` text NOT NULL,
  `source_file` varchar(255) NOT NULL,
  `source_sheet` varchar(64) NOT NULL,
  `source_row_no` int NOT NULL,
  `source_file_sha256` char(64) NOT NULL,
  `dedup_key` char(64) NOT NULL,
  `row_hash` char(64) NOT NULL,
  `raw_payload_json` longtext NOT NULL,
  `loaded_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_raw_meeting_event` (`dedup_key`),
  KEY `idx_raw_meeting_period_class` (`period_ym`, `therapeutic_class`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""
