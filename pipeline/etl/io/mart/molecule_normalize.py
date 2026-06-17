"""Molecule normalization for dynamic market filters.

The bridge table indexes molecules by a compact, deterministic key so runtime
queries can use indexed equality instead of free-form ``LIKE`` scans.  Inputs
come from mart JSON dimensions and catalog parquet, where molecule text is a
mix of English names, catalog abbreviations (for example ``PTV/EZE``), and
multi-ingredient formulas.

The rules below are deliberately conservative:
- split only on molecule-list separators that appear in source data;
- fold salts/abbreviations that the MI Master or resolver already uses;
- keep nutrition/electrolyte ingredients as indexed components instead of
  trying to infer therapeutic equivalence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
import re
import unicodedata


@dataclass(frozen=True, slots=True)
class MoleculeComponent:
    """One normalized molecule component extracted from a raw molecule cell."""

    raw: str
    norm: str
    display: str
    index: int
    total: int


MISSING_TEXTS: Final[frozenset[str]] = frozenset({"", "nan", "none", "null", "<na>", "#n/a", "n/a", "na"})
MOLECULE_SEPARATOR_RE: Final[re.Pattern[str]] = re.compile(
    r"\s*(?:\+|＋|\||;|,|/|&|\band\b)\s*",
    re.IGNORECASE,
)
PUNCTUATION_RE: Final[re.Pattern[str]] = re.compile(r"[^0-9a-z가-힣]+", re.IGNORECASE)

# These aliases are source-backed.  ``PTV/EZE`` and ``PTV/Fenofibric acid`` are
# present in strategic_brand for Livalo combination products, while
# ``Iron scurose`` is a catalog typo already observed for 베노훼럼.  The salt
# folds connect raw IQVIA salt labels to the same base ingredient a user will
# enter in the dynamic molecule filter.
SYNONYM_BY_KEY: Final[dict[str, str]] = {
    "ptv": "pitavastatin",
    "피타바스타틴": "pitavastatin",
    "pitavastatin calcium": "pitavastatin",
    "pitavastatin calcium hydrate": "pitavastatin",
    "eze": "ezetimibe",
    "에제티미브": "ezetimibe",
    "fenofibric acid": "fenofibrate",
    "페노피브레이트": "fenofibrate",
    "amlodipine besylate": "amlodipine",
    "암로디핀": "amlodipine",
    "valsartan": "valsartan",
    "발사르탄": "valsartan",
    "anagliptin": "anagliptin",
    "아나글립틴": "anagliptin",
    "metformin hydrochloride": "metformin",
    "메트포르민": "metformin",
    "rabeprazole sodium": "rabeprazole",
    "라베프라졸": "rabeprazole",
    "esomeprazole magnesium": "esomeprazole",
    "esomeprazole magnesium trihydrate": "esomeprazole",
    "calcium carbonate p p t": "calcium carbonate",
    "iron scurose": "iron sucrose",
}

DISPLAY_BY_NORM: Final[dict[str, str]] = {
    "pitavastatin": "Pitavastatin",
    "ezetimibe": "Ezetimibe",
    "fenofibrate": "Fenofibrate",
    "amlodipine": "Amlodipine",
    "valsartan": "Valsartan",
    "anagliptin": "Anagliptin",
    "metformin": "Metformin",
    "rabeprazole": "Rabeprazole",
    "esomeprazole": "Esomeprazole",
    "iron sucrose": "Iron sucrose",
}


def clean_molecule_text(value: str | None) -> str | None:
    """Return a trimmed molecule string, or ``None`` for missing placeholders."""

    if value is None:
        return None
    text = unicodedata.normalize("NFKC", str(value)).strip()
    if text.lower() in MISSING_TEXTS:
        return None
    return re.sub(r"\s+", " ", text)


def _molecule_key(value: str) -> str:
    """Normalize punctuation/casing before alias lookup."""

    text = unicodedata.normalize("NFKC", value).strip().lower()
    text = PUNCTUATION_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_molecule_component(value: str) -> str:
    """Return the indexed molecule key for one already-split component."""

    key = _molecule_key(value)
    if " as " in key:
        # IQVIA molecule labels sometimes encode salt/base as
        # "amlodipine besylate as amlodipine".  For molecule filtering the
        # query term should hit the active moiety, so the normalized right-hand
        # side is preferred when it has a known conservative alias.
        left, right = key.split(" as ", 1)
        right_norm = SYNONYM_BY_KEY.get(right, right)
        if right_norm in DISPLAY_BY_NORM:
            return right_norm
        left_norm = SYNONYM_BY_KEY.get(left, left)
        if left_norm in DISPLAY_BY_NORM:
            return left_norm
    return SYNONYM_BY_KEY.get(key, key)


def molecule_display_name(norm: str, raw: str) -> str:
    """Return a stable display label for a normalized molecule key."""

    if norm in DISPLAY_BY_NORM:
        return DISPLAY_BY_NORM[norm]
    cleaned = clean_molecule_text(raw)
    return cleaned or norm


def split_molecule_components(value: str | None) -> tuple[MoleculeComponent, ...]:
    """Split one molecule cell into normalized searchable components.

    Combination products are represented as one raw molecule string with a
    separator (``+`` or ``/`` in the MI Master canonical rows; ``+`` in IQVIA
    molecule descriptions).  The bridge stores one row per component so a
    dynamic filter for either ingredient can find the combination brand.
    Duplicate components in one source cell are collapsed after normalization.
    """

    cleaned = clean_molecule_text(value)
    if cleaned is None:
        return ()
    raw_parts = [part for part in MOLECULE_SEPARATOR_RE.split(cleaned) if clean_molecule_text(part)]
    if not raw_parts:
        raw_parts = [cleaned]

    seen: set[str] = set()
    components: list[MoleculeComponent] = []
    total = len(raw_parts)
    for raw in raw_parts:
        norm = normalize_molecule_component(raw)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        components.append(
            MoleculeComponent(
                raw=clean_molecule_text(raw) or raw,
                norm=norm,
                display=molecule_display_name(norm, raw),
                index=len(components) + 1,
                total=total,
            )
        )
    return tuple(components)


def normalize_molecule_query(value: str | None) -> str | None:
    """Normalize user/API molecule filter text with the same bridge rules."""

    parts = split_molecule_components(value)
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0].norm
    return " ".join(part.norm for part in parts)
