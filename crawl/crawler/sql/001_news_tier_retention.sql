-- 2-tier crawl metadata and retention support.
-- Review and apply manually after backup. Existing rows are Tier1 by default.

ALTER TABLE news_raw
  ADD COLUMN IF NOT EXISTS tier TINYINT NOT NULL DEFAULT 1 AFTER scored_at,
  ADD COLUMN IF NOT EXISTS collected_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP AFTER tier;

ALTER TABLE events
  ADD COLUMN IF NOT EXISTS tier TINYINT NOT NULL DEFAULT 1 AFTER search_keyword,
  ADD COLUMN IF NOT EXISTS collected_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP AFTER tier;

ALTER TABLE event_brand_scores
  ADD COLUMN IF NOT EXISTS tier TINYINT NOT NULL DEFAULT 1 AFTER llm_meta,
  ADD COLUMN IF NOT EXISTS collected_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP AFTER tier;

CREATE INDEX IF NOT EXISTS idx_news_raw_tier_collected_at
  ON news_raw (tier, collected_at);
CREATE INDEX IF NOT EXISTS idx_events_tier_collected_at
  ON events (tier, collected_at);
CREATE INDEX IF NOT EXISTS idx_event_brand_scores_tier_collected_at
  ON event_brand_scores (tier, collected_at);

-- Tier2 rolling retention. Run only after confirming the cutoff and backup.
-- Keep Tier1 for five years; keep Tier2 for one rolling year.
-- DELETE FROM event_brand_scores
-- WHERE tier = 2 AND collected_at < (CURRENT_TIMESTAMP - INTERVAL 1 YEAR);
--
-- DELETE e
-- FROM events e
-- LEFT JOIN event_brand_scores s ON s.event_id = e.event_id
-- WHERE e.tier = 2
--   AND e.collected_at < (CURRENT_TIMESTAMP - INTERVAL 1 YEAR)
--   AND s.event_id IS NULL;
--
-- DELETE n
-- FROM news_raw n
-- LEFT JOIN events e ON e.news_id = n.news_id
-- WHERE n.tier = 2
--   AND n.collected_at < (CURRENT_TIMESTAMP - INTERVAL 1 YEAR)
--   AND e.news_id IS NULL;
