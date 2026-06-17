"""Public facade for the dynamic brand-molecule bridge.

Keep this module tiny: production callers import from here, while extraction,
normalization, and DB build responsibilities live in focused sibling modules.
"""

from __future__ import annotations

from pipeline.etl.io.mart.molecule_bridge_build import build_molecule_bridge
from pipeline.etl.io.mart.molecule_normalize import normalize_molecule_query, split_molecule_components

__all__ = ["build_molecule_bridge", "normalize_molecule_query", "split_molecule_components"]
