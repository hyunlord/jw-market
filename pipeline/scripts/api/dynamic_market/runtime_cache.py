"""Shared persistent response cache used by general and strategic cause routes."""

from __future__ import annotations

from pipeline.scripts.api.config import config
from pipeline.scripts.api.dynamic_market.response_cache import DynamicResponseCache, MySQLDynamicResponseCacheStore


dynamic_response_cache = DynamicResponseCache(
    store=MySQLDynamicResponseCacheStore(
        mart_db=config.db_name,
        general_dimension_db=config.general_dimension_db_name,
        strategic_dimension_db=config.strategic_dimension_db_name,
        ttl_seconds=config.cache_ttl_seconds,
    )
)
