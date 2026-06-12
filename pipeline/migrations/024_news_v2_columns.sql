-- Stage GA2 Agent 2 news corpus v2 columns.
-- Adds the local dev schema columns expected by corpus_loader_v2.py.
-- article_url UNIQUE and event_brand_scores(news_id) FK are intentionally
-- excluded until the news dedup/backfill policy is approved.

ALTER TABLE news_raw
  ADD COLUMN matched_search_keywords LONGTEXT NULL AFTER search_keyword,
  ADD COLUMN matched_jw_search_contexts LONGTEXT NULL AFTER matched_search_keywords,
  ADD COLUMN news_source_file VARCHAR(1000) NULL AFTER matched_jw_search_contexts,
  ADD COLUMN scored TINYINT(1) NOT NULL DEFAULT 0 AFTER news_source_file,
  ADD COLUMN scored_at DATETIME NULL AFTER scored;

ALTER TABLE event_brand_scores
  ADD COLUMN news_id VARCHAR(64) NULL AFTER event_id,
  ADD COLUMN derivation VARCHAR(32) NOT NULL DEFAULT 'llm_direct' AFTER generated_at,
  ADD COLUMN mirrored_from_jw_brands LONGTEXT NULL AFTER derivation,
  ADD COLUMN tag VARCHAR(50) NULL AFTER mirrored_from_jw_brands,
  ADD COLUMN summary TEXT NULL AFTER tag,
  ADD COLUMN workflow_id INT NULL AFTER summary,
  ADD COLUMN catalog_version VARCHAR(64) NULL AFTER workflow_id,
  ADD COLUMN llm_meta LONGTEXT NULL AFTER catalog_version,
  ADD KEY idx_event_brand_scores_news_derivation (news_id, derivation),
  ADD KEY idx_event_brand_scores_derivation (derivation);
