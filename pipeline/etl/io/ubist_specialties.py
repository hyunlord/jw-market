"""Shared raw UBIST specialty hierarchy catalog access."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


CUSTOMER_DICTIONARY_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "customer_dictionary.yaml"
)


def specialty_hierarchies(
    customer_dict: Mapping[str, Any] | None = None,
) -> dict[str, tuple[str, ...]]:
    """Return validated aggregate-parent relationships from the ETL catalog."""
    raw = (
        customer_dict.get("ubist_specialty_hierarchies")
        if customer_dict is not None
        else _default_hierarchies()
    )
    if not isinstance(raw, Mapping) or not raw:
        raise RuntimeError("customer dictionary requires ubist_specialty_hierarchies")
    result: dict[str, tuple[str, ...]] = {}
    for parent, children in raw.items():
        parent_text = str(parent).strip()
        if not parent_text or not isinstance(children, Sequence) or isinstance(children, (str, bytes)):
            raise RuntimeError("invalid UBIST specialty hierarchy catalog entry")
        child_values = tuple(str(child).strip() for child in children if str(child).strip())
        if not child_values:
            raise RuntimeError(f"UBIST specialty hierarchy has no children: {parent_text}")
        result[parent_text] = child_values
    return result


@lru_cache(maxsize=1)
def _default_hierarchies() -> Mapping[str, Any]:
    with CUSTOMER_DICTIONARY_PATH.open(encoding="utf-8") as stream:
        document = yaml.safe_load(stream) or {}
    return document.get("ubist_specialty_hierarchies", {})


def aggregate_specialty_labels(
    customer_dict: Mapping[str, Any] | None = None,
) -> frozenset[str]:
    """Return raw specialty labels that duplicate their catalogued children."""
    return frozenset(specialty_hierarchies(customer_dict))


def detail_specialty_labels(
    customer_dict: Mapping[str, Any] | None = None,
) -> tuple[str, ...]:
    """Return ordered atomic specialties referenced by aggregate parents."""
    return tuple(
        dict.fromkeys(
            child
            for children in specialty_hierarchies(customer_dict).values()
            for child in children
        )
    )
