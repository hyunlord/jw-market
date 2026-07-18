ALTER TABLE agent3_brand_strength
  ADD COLUMN IF NOT EXISTS serving_brand_name VARCHAR(255) NULL AFTER brand_name;

CREATE UNIQUE INDEX IF NOT EXISTS uq_agent3_brand_strength_serving_brand_name
  ON agent3_brand_strength (serving_brand_name);
