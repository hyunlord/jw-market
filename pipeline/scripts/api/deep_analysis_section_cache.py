"""Bounded on-demand cache for strategic forecast and simulation sections."""

from __future__ import annotations

from pipeline.scripts.api.config import config
from pipeline.scripts.api.dynamic_market.response_cache import DynamicResponseCache, MySQLDynamicResponseCacheStore


deep_section_cache = DynamicResponseCache(
    store=MySQLDynamicResponseCacheStore(
        mart_db=config.db_name,
        general_dimension_db=config.general_dimension_db_name,
        strategic_dimension_db=config.strategic_dimension_db_name,
        ttl_seconds=config.cache_ttl_seconds,
        max_rows=2_000,
        max_bytes=256 * 1024 * 1024,
        namespace="deep_expensive",
    ),
    cache_write_mode=config.cache_write_mode,
)
