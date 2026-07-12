ALTER TABLE mart_analysis_level_block
  ADD COLUMN profile_sig CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT '' AFTER measure,
  ADD COLUMN trim_mode VARCHAR(8) NOT NULL DEFAULT 'full' AFTER profile_sig,
  DROP PRIMARY KEY,
  ADD PRIMARY KEY (view, market_id, source, measure, profile_sig, trim_mode);
