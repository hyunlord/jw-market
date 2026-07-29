"""Compatibility exports for molecule normalization.

New cross-layer callers should import :mod:`pipeline.domain.molecules`.
"""

from pipeline.domain.molecules import (
    DISPLAY_BY_NORM,
    MISSING_TEXTS,
    MOLECULE_SEPARATOR_RE,
    PUNCTUATION_RE,
    SYNONYM_BY_KEY,
    MoleculeComponent,
    clean_molecule_text,
    molecule_display_name,
    normalize_molecule_component,
    normalize_molecule_query,
    split_molecule_components,
)


__all__ = (
    "DISPLAY_BY_NORM",
    "MISSING_TEXTS",
    "MOLECULE_SEPARATOR_RE",
    "PUNCTUATION_RE",
    "SYNONYM_BY_KEY",
    "MoleculeComponent",
    "clean_molecule_text",
    "molecule_display_name",
    "normalize_molecule_component",
    "normalize_molecule_query",
    "split_molecule_components",
)
