"""Derive view-specific brand sources from serving-context data presence."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pipeline.scripts.api.deep_analysis_context import (
    DeepAnalysisContextError,
    public_source_labels,
    resolve_deep_analysis_context,
)


ContextResolver = Callable[..., Any]


def brand_source_options(
    brand: str,
    *,
    resolver: ContextResolver = resolve_deep_analysis_context,
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Return public contexts plus general and strategic sources with data."""

    contexts: list[dict[str, Any]] = []
    general: set[str] = set()
    strategic: set[str] = set()
    for view_kind in ("general", "strategic_ml", "strategic_cd"):
        try:
            resolved = resolver(
                brand=brand,
                view_kind=view_kind,
                market_id=None,
                source=None,
            )
            available = [resolved.public()]
        except DeepAnalysisContextError as exc:
            available = list(exc.available_contexts)
        for context in available:
            source = str(context.get("source") or "").strip()
            if source and bool(context.get("has_market_data")):
                (general if view_kind == "general" else strategic).add(source)
            public = {
                "view_kind": context.get("view_kind"),
                "market_id": context.get("market_id"),
                "market_name": context.get("market_name"),
                "has_market_data": bool(context.get("has_market_data")),
            }
            if "is_primary" in context:
                public["is_primary"] = bool(context.get("is_primary"))
            if public["market_id"] and public not in contexts:
                contexts.append(public)
    return contexts, public_source_labels(general), public_source_labels(strategic)
