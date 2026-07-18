ALTER TABLE cache_dynamic_market_response
    ADD COLUMN IF NOT EXISTS namespace VARCHAR(32) NOT NULL DEFAULT 'dynamic' AFTER cache_key,
    ADD INDEX IF NOT EXISTS idx_dynamic_response_namespace_eviction
        (namespace, state, hit_count, last_hit_at, updated_at);
