from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path
import re
import unicodedata
from typing import Final, TypedDict


class HiraMapping(TypedDict):
    sick_cd: str
    disease_name: str
    basis: str


class HiraCatalogEntry(TypedDict):
    sick_cd: str
    disease_name: str
    ingredients: list[str]
    disease_terms: list[str]
    basis: str


@dataclass(frozen=True, slots=True)
class ResolvedHiraMapping:
    mapping: HiraMapping
    matched_ingredients: tuple[str, ...]
    mapping_source: str


@dataclass(frozen=True, slots=True)
class HiraCatalogError(ValueError):
    detail: str

    def __str__(self) -> str:
        return self.detail


_CATALOG_PATH: Final = Path(__file__).resolve().parents[1] / "fixtures" / "hira_ingredient_disease_catalog.json"
_UNBRANDED_QUERY_TERMS: Final = (
    "요양기관종별",
    "가이드라인",
    "알려주세요",
    "보여주세요",
    "환자통계",
    "환자분포",
    "질병통계",
    "질환통계",
    "연도별",
    "연령별",
    "지역별",
    "환자수",
    "알려줘",
    "보여줘",
    "해주세요",
    "질환",
    "질병",
    "환자",
    "통계",
    "분포",
    "추이",
    "현황",
    "성별",
    "입원",
    "외래",
    "관련",
    "기준",
    "치료",
    "지침",
    "근거",
    "최신",
    "nccn",
    "국내",
    "전국",
    "최근",
    "확인",
    "해줘",
    "어때",
    "얼마",
    "몇명",
    "명",
    "년",
    "월",
)
_UNBRANDED_QUERY_PARTICLES: Final = frozenset(("", "의", "은", "는", "이", "가", "을", "를"))


def mappings_for_ingredients(
    ingredients: tuple[str, ...],
) -> tuple[tuple[ResolvedHiraMapping, ...], tuple[str, ...]]:
    by_ingredient = _catalog_by_ingredient()
    resolved: dict[str, ResolvedHiraMapping] = {}
    unmapped: list[str] = []
    for ingredient in ingredients:
        entries = by_ingredient.get(_normalize_lookup(ingredient), ())
        if not entries:
            unmapped.append(ingredient)
            continue
        for entry in entries:
            sick_cd = entry["sick_cd"]
            current = resolved.get(sick_cd)
            matched = (ingredient,) if current is None else (*current.matched_ingredients, ingredient)
            resolved[sick_cd] = ResolvedHiraMapping(
                mapping=_mapping_from_entry(entry),
                matched_ingredients=tuple(dict.fromkeys(matched)),
                mapping_source="ingredient_disease_dictionary",
            )
    if unmapped:
        return (), tuple(unmapped)
    return tuple(resolved.values()), ()


def mapping_for_unbranded_query(text: str) -> ResolvedHiraMapping | None:
    match = _catalog_text_match(text)
    if match is None:
        return None
    entry, matched_term = match
    residue = _normalize_lookup(text).replace(_normalize_lookup(matched_term), "", 1)
    for term in _UNBRANDED_QUERY_TERMS:
        residue = residue.replace(_normalize_lookup(term), "")
    residue = re.sub(r"[^0-9a-z가-힣]+", "", residue)
    residue = re.sub(r"\d+", "", residue)
    if residue not in _UNBRANDED_QUERY_PARTICLES:
        return None
    return ResolvedHiraMapping(
        mapping=_mapping_from_entry(entry),
        matched_ingredients=(),
        mapping_source="disease_term_dictionary",
    )


def hira_disease_code_for_text(text: str) -> str | None:
    """Return one KCD code for an explicit code or a pure disease query."""

    candidate = text.strip().upper()
    if re.fullmatch(r"[A-Z]\d{2}(?:\.\d{1,2})?", candidate):
        return candidate
    return hira_disease_code_for_unbranded_query(text)


def hira_disease_subject_for_unbranded_query(text: str) -> str | None:
    mapping = mapping_for_unbranded_query(text)
    return mapping.mapping["disease_name"] if mapping is not None else None


def hira_disease_code_for_unbranded_query(text: str) -> str | None:
    mapping = mapping_for_unbranded_query(text)
    return mapping.mapping["sick_cd"] if mapping is not None else None


def _catalog_text_match(text: str) -> tuple[HiraCatalogEntry, str] | None:
    normalized = _normalize_lookup(text)
    candidates = (
        (entry, term)
        for entry in _catalog_entries()
        for term in entry["disease_terms"]
        if _normalize_lookup(term) in normalized
    )
    return max(candidates, key=lambda item: len(_normalize_lookup(item[1])), default=None)


def _mapping_from_entry(entry: HiraCatalogEntry) -> HiraMapping:
    return {
        "sick_cd": entry["sick_cd"],
        "disease_name": entry["disease_name"],
        "basis": entry["basis"],
    }


@lru_cache(maxsize=1)
def _catalog_entries() -> tuple[HiraCatalogEntry, ...]:
    payload = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not payload:
        raise HiraCatalogError("HIRA ingredient disease catalog must be a non-empty list")
    entries: list[HiraCatalogEntry] = []
    for raw in payload:
        if not isinstance(raw, dict):
            raise HiraCatalogError("HIRA ingredient disease catalog rows must be objects")
        sick_cd = str(raw.get("sick_cd") or "").strip().upper()
        disease_name = str(raw.get("disease_name") or "").strip()
        ingredients = [str(value).strip() for value in raw.get("ingredients", []) if str(value).strip()]
        disease_terms = [str(value).strip() for value in raw.get("disease_terms", []) if str(value).strip()]
        basis = str(raw.get("basis") or "").strip()
        if not re.fullmatch(r"[A-Z]\d{2}(?:\.\d{1,2})?", sick_cd):
            raise HiraCatalogError(f"invalid HIRA sick_cd: {sick_cd!r}")
        if not disease_name or not ingredients or not basis:
            raise HiraCatalogError(f"incomplete HIRA ingredient disease catalog row: {sick_cd}")
        entries.append(
            HiraCatalogEntry(
                sick_cd=sick_cd,
                disease_name=disease_name,
                ingredients=ingredients,
                disease_terms=disease_terms,
                basis=basis,
            )
        )
    return tuple(entries)


@lru_cache(maxsize=1)
def _catalog_by_ingredient() -> dict[str, tuple[HiraCatalogEntry, ...]]:
    grouped: dict[str, list[HiraCatalogEntry]] = {}
    for entry in _catalog_entries():
        for ingredient in entry["ingredients"]:
            grouped.setdefault(_normalize_lookup(ingredient), []).append(entry)
    return {key: tuple(value) for key, value in grouped.items()}


def _normalize_lookup(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    return re.sub(r"[\s_+\-/]+", "", normalized).casefold()
