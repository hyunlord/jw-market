from __future__ import annotations

from collections.abc import Mapping
import json
import os
from typing import Literal, TypeAlias

from jw_chat_agent_poc.tools.external.client import (
    WEB_SEARCH_PROVIDER_ENV,
    ExternalApiClient,
    ExternalCall,
    _bounded_web_results,
    _mcp_tool_spec,
)
from jw_chat_agent_poc.tools.external.result_cache import ExternalResultCache


EXTERNAL_RESULT_CACHE_TTL_ENV = "CHAT_EXTERNAL_RESULT_CACHE_TTL_SECONDS"
EXTERNAL_RESULT_CACHE_MAX_ENTRIES_ENV = "CHAT_EXTERNAL_RESULT_CACHE_MAX_ENTRIES"

CacheScalar: TypeAlias = str | int | float | bool | None


class CachedExternalApiClient(ExternalApiClient):
    def __init__(self, *, result_cache: ExternalResultCache, timeout_s: int = 12) -> None:
        super().__init__(mode="live", timeout_s=timeout_s)
        self._result_cache = result_cache

    def _live_mcp_call(self, tool: str, params: dict[str, str]) -> ExternalCall:
        spec = _mcp_tool_spec(tool, params)
        url = self._mcp_url(spec["resource_id"], spec["source"])
        key = (
            "mcp",
            self.redact_url(url),
            spec["mcp_tool"],
            _canonical_arguments(spec["arguments"]),
        )
        cached = self._result_cache.get(key)
        if cached is not None:
            return cached
        call = super()._live_mcp_call(tool, params)
        self._result_cache.put(key, call)
        return call

    def _live_web_search(
        self,
        query: str,
        max_results: int = 5,
        *,
        topic: Literal["general", "news"] = "general",
    ) -> ExternalCall:
        provider = os.environ.get(WEB_SEARCH_PROVIDER_ENV, "tavily").strip().lower()
        key = (
            "web",
            provider,
            " ".join(query.split()),
            _bounded_web_results(max_results),
            topic,
        )
        cached = self._result_cache.get(key)
        if cached is not None:
            return cached
        call = super()._live_web_search(query, max_results=max_results, topic=topic)
        self._result_cache.put(key, call)
        return call


def _canonical_arguments(arguments: Mapping[str, CacheScalar]) -> str:
    normalized = {
        key: " ".join(value.split()) if isinstance(value, str) else value
        for key, value in arguments.items()
    }
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
