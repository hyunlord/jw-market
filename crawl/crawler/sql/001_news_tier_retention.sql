-- 2-tier crawl metadata and retention support.
-- Review and apply manually after backup. Existing rows are Tier1 by default.

ALTER TABLE news_raw
  ADD COLUMN IF NOT EXISTS tier TINYINT NOT NULL DEFAULT 1 AFTER scored_at,
  ADD COLUMN IF NOT EXISTS collected_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP AFTER tier,
  ADD COLUMN IF NOT EXISTS expire_at DATETIME NULL AFTER collected_at;

ALTER TABLE events
  ADD COLUMN IF NOT EXISTS tier TINYINT NOT NULL DEFAULT 1 AFTER search_keyword,
  ADD COLUMN IF NOT EXISTS collected_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP AFTER tier,
  ADD COLUMN IF NOT EXISTS expire_at DATETIME NULL AFTER collected_at;

ALTER TABLE event_brand_scores
  ADD COLUMN IF NOT EXISTS tier TINYINT NOT NULL DEFAULT 1 AFTER llm_meta,
  ADD COLUMN IF NOT EXISTS collected_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP AFTER tier,
  ADD COLUMN IF NOT EXISTS expire_at DATETIME NULL AFTER collected_at;

CREATE INDEX IF NOT EXISTS idx_news_raw_tier_collected_at
  ON news_raw (tier, collected_at);
CREATE INDEX IF NOT EXISTS idx_events_tier_collected_at
  ON events (tier, collected_at);
CREATE INDEX IF NOT EXISTS idx_event_brand_scores_tier_collected_at
  ON event_brand_scores (tier, collected_at);
CREATE INDEX IF NOT EXISTS idx_news_raw_tier_expire_at
  ON news_raw (tier, expire_at);
CREATE INDEX IF NOT EXISTS idx_events_tier_expire_at
  ON events (tier, expire_at);
CREATE INDEX IF NOT EXISTS idx_event_brand_scores_tier_expire_at
  ON event_brand_scores (tier, expire_at);

-- Tier2 rolling retention. Run only after confirming the cutoff and backup.
-- New rows carry expire_at at load time: Tier1 = collected_at + 5 years,
-- Tier2 = collected_at + 1 year. Legacy rows with expire_at NULL are retained.
-- DELETE FROM event_brand_scores
-- WHERE expire_at IS NOT NULL AND expire_at < CURRENT_TIMESTAMP;
--
-- DELETE e
-- FROM events e
-- LEFT JOIN event_brand_scores s ON s.event_id = e.event_id
-- WHERE e.expire_at IS NOT NULL
--   AND e.expire_at < CURRENT_TIMESTAMP
--   AND s.event_id IS NULL;
--
-- DELETE n
-- FROM news_raw n
-- LEFT JOIN events e ON e.news_id = n.news_id
-- WHERE n.expire_at IS NOT NULL
--   AND n.expire_at < CURRENT_TIMESTAMP
--   AND e.news_id IS NULL;
