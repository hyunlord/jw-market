-- Additive producer-side lineage for independently generated short/long variants.
ALTER TABLE cache_deep_analysis_ai_analysis ADD COLUMN brand_key VARCHAR(255) NULL AFTER brand;
ALTER TABLE cache_deep_analysis_ai_analysis ADD COLUMN short_workflow_id INT NULL;
ALTER TABLE cache_deep_analysis_ai_analysis ADD COLUMN short_workflow_revision_id INT NULL;
ALTER TABLE cache_deep_analysis_ai_analysis ADD COLUMN short_generation_id VARCHAR(255) NULL;
ALTER TABLE cache_deep_analysis_ai_analysis ADD COLUMN short_input_hash CHAR(64) NULL;
ALTER TABLE cache_deep_analysis_ai_analysis ADD COLUMN short_generated_at DATETIME(6) NULL;
ALTER TABLE cache_deep_analysis_ai_analysis ADD COLUMN short_source_epoch VARCHAR(255) NULL;
ALTER TABLE cache_deep_analysis_ai_analysis ADD COLUMN short_generation_status VARCHAR(32) NULL;
ALTER TABLE cache_deep_analysis_ai_analysis ADD COLUMN long_workflow_id INT NULL;
ALTER TABLE cache_deep_analysis_ai_analysis ADD COLUMN long_workflow_revision_id INT NULL;
ALTER TABLE cache_deep_analysis_ai_analysis ADD COLUMN long_generation_id VARCHAR(255) NULL;
ALTER TABLE cache_deep_analysis_ai_analysis ADD COLUMN long_input_hash CHAR(64) NULL;
ALTER TABLE cache_deep_analysis_ai_analysis ADD COLUMN long_generated_at DATETIME(6) NULL;
ALTER TABLE cache_deep_analysis_ai_analysis ADD COLUMN long_source_epoch VARCHAR(255) NULL;
ALTER TABLE cache_deep_analysis_ai_analysis ADD COLUMN long_generation_status VARCHAR(32) NULL;
