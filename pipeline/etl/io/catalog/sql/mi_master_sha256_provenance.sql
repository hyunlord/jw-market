-- MI Master definition refresh provenance DDL artifact.
-- Do not execute from this repository change; apply only through the approved DB rollout path.
-- Replace {{APPROVED_CATALOG_SCHEMA}} only after explicit DB rollout approval.

ALTER TABLE `{{APPROVED_CATALOG_SCHEMA}}`.`catalog_ml_market`
  ADD COLUMN IF NOT EXISTS `mi_master_sha256` CHAR(64) NULL AFTER `source_file_version`;

ALTER TABLE `{{APPROVED_CATALOG_SCHEMA}}`.`catalog_cd_market`
  ADD COLUMN IF NOT EXISTS `mi_master_sha256` CHAR(64) NULL AFTER `source_file_version`;

ALTER TABLE `{{APPROVED_CATALOG_SCHEMA}}`.`catalog_strategic_brand`
  ADD COLUMN IF NOT EXISTS `mi_master_sha256` CHAR(64) NULL AFTER `source_file_version`;
