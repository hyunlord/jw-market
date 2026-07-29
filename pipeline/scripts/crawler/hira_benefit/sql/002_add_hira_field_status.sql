ALTER TABLE hira_benefit_notice
  ADD COLUMN target_status VARCHAR(16) DEFAULT NULL AFTER dosage_limit,
  ADD COLUMN exclusion_status VARCHAR(16) DEFAULT NULL AFTER target_status,
  ADD COLUMN dosage_status VARCHAR(16) DEFAULT NULL AFTER exclusion_status;
