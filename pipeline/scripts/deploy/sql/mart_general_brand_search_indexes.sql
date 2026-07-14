ALTER TABLE mart_general_brand_metric
  ADD INDEX idx_general_brand_name (brand_name, measure),
  ALGORITHM=INPLACE,
  LOCK=NONE;
