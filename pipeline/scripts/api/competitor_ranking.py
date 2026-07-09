"""Shared competitor selection by scoped total sales."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class CompetitorRankItem(Generic[T]):
    """One ranked item with the payload its caller needs back."""

    brand_key: str
    total_value: float
    payload: T


MAX_COMPETITOR_COUNT = 5


def select_top_competitors(
    items: tuple[CompetitorRankItem[T], ...],
    *,
    selected_brand_key: str | None,
    top_n: int = MAX_COMPETITOR_COUNT,
) -> tuple[T, ...]:
    """Return selected brand first, then top competitors by total sales."""

    selected = next((item for item in items if selected_brand_key and item.brand_key == selected_brand_key), None)
    competitors = sorted(
        (item for item in items if item is not selected),
        key=lambda item: (-item.total_value, item.brand_key),
    )
    ordered = ([selected] if selected else []) + competitors[:top_n]
    return tuple(item.payload for item in ordered if item is not None)
