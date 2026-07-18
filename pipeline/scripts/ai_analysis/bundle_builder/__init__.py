from __future__ import annotations

import importlib
from typing import Any

__all__ = [
    "BundleConfig",
    "build_brand_bundle",
    "build_general_brand_bundle",
    "render_narrative",
    "compute_mat_12m_absolute",
    "recompute_ms_pct",
]


def __getattr__(name: str) -> Any:
    match name:
        case "BundleConfig":
            from .config import BundleConfig

            return BundleConfig
        case "build_brand_bundle":
            from .orchestrator import build_brand_bundle

            return build_brand_bundle
        case "build_general_brand_bundle":
            from .general_bundle_adapter import build_general_brand_bundle

            return build_general_brand_bundle
        case "render_narrative":
            from .prompt_renderer import render_narrative

            return render_narrative
        case "compute_mat_12m_absolute":
            from .mat_computer import compute_mat_12m_absolute

            return compute_mat_12m_absolute
        case "recompute_ms_pct":
            from .ms_recomputer import recompute_ms_pct

            return recompute_ms_pct
        case "event_bundle_builder" | "kpi_provider":
            return importlib.import_module(f"{__name__}.{name}")
        case _:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
