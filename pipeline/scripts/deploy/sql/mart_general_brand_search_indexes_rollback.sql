ALTER TABLE mart_general_brand_metric
  DROP INDEX idx_general_brand_name,
  ALGORITHM=INPLACE,
  LOCK=NONE;
