ALTER TABLE cache_dynamic_market_response
    ADD INDEX IF NOT EXISTS idx_dynamic_response_eviction
    (state, hit_count, last_hit_at, updated_at);
