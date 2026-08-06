-- MI Master definition refresh provenance DDL artifact.
-- Do not execute from this repository change; apply only through the approved DB rollout path.

ALTER TABLE `catalog_ml_market`
  ADD COLUMN `mi_master_sha256` CHAR(64) NULL AFTER `source_file_version`;

ALTER TABLE `catalog_cd_market`
  ADD COLUMN `mi_master_sha256` CHAR(64) NULL AFTER `source_file_version`;

ALTER TABLE `catalog_strategic_brand`
  ADD COLUMN `mi_master_sha256` CHAR(64) NULL AFTER `source_file_version`;
