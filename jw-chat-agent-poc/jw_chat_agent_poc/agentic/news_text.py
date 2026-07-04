from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
from typing import Literal


TextOperator = Literal["AND", "OR"]


@dataclass(frozen=True, slots=True)
class TextSearchSpec:
    terms: tuple[str, ...]
    operator: TextOperator

    def label(self) -> str:
        joiner = f" {self.operator} "
        return joiner.join(self.terms)


def parse_text_search(value: str) -> TextSearchSpec | None:
    text = _clean_query_tail(value)
    if not text:
        return None
    operator = _operator(text)
    terms = tuple(term for term in (_term_clean(item) for item in _split_terms(text, operator)) if term)
    if not terms:
        return None
    return TextSearchSpec(terms=terms, operator=operator)


def normalized_contains(text: str, spec: TextSearchSpec) -> bool:
    target = _normalize_for_search(text)
    if not target:
        return False
    checks = tuple(_normalize_for_search(term) in target for term in spec.terms)
    if spec.operator == "AND":
        return all(checks)
    return any(checks)


def _operator(text: str) -> TextOperator:
    if "+" in text or re.search(r"\bAND\b|그리고|및|둘\s*다|모두", text, flags=re.IGNORECASE):
        return "AND"
    return "OR"


def _split_terms(text: str, operator: TextOperator) -> tuple[str, ...]:
    if operator == "AND":
        return tuple(re.split(r"\s*(?:\+|\bAND\b|그리고|및|둘\s*다|모두)\s*", text, flags=re.IGNORECASE))
    return tuple(re.split(r"\s*(?:\||\bOR\b|또는|혹은)\s*", text, flags=re.IGNORECASE))


def _clean_query_tail(value: str) -> str:
    text = value.strip()
    text = re.sub(r"\s*(있는\s*거|있는거|들어간\s*거|들어간거|포함.*|것만|만\s*보여.*)$", "", text)
    return text.strip()


def _term_clean(value: str) -> str:
    return value.strip(" \t\r\n,.;:()[]{}\"'")


def _normalize_for_search(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"\s+", "", normalized)
