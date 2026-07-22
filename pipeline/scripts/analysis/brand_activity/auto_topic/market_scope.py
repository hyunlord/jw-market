from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, TypeAlias


TargetMode: TypeAlias = Literal["existing", "all", "uncovered", "explicit"]


class TargetSelectionError(ValueError):
    """Raised when an ATC target request cannot be resolved from keyword data."""


def select_target_markets(
    *,
    available_markets: Sequence[str],
    covered_markets: Sequence[str],
    mode: TargetMode,
    explicit_markets: Sequence[str] = (),
) -> tuple[str, ...]:
    """Select deterministic ATC targets from live keyword and topic inventories."""
    available = tuple(sorted(set(available_markets)))
    available_set = set(available)
    covered_set = set(covered_markets)
    explicit = tuple(sorted(set(explicit_markets)))
    if explicit and mode != "explicit":
        raise TargetSelectionError("--target-atc4 requires --target-mode explicit")
    unknown = tuple(market for market in explicit if market not in available_set)
    if unknown:
        raise TargetSelectionError(f"target ATC has no keyword data: {', '.join(unknown)}")
    overlap = tuple(market for market in explicit if market in covered_set)
    if overlap:
        raise TargetSelectionError(f"target ATC already has topic scope: {', '.join(overlap)}")

    match mode:
        case "existing":
            return tuple(market for market in available if market in covered_set)
        case "all":
            return available
        case "uncovered":
            return tuple(market for market in available if market not in covered_set)
        case "explicit":
            if not explicit:
                raise TargetSelectionError("explicit target mode requires --target-atc4")
            return explicit
        case unreachable:
            raise TargetSelectionError(f"unsupported target mode: {unreachable}")


def parse_target_markets(value: str) -> tuple[str, ...]:
    """Parse a comma-separated CLI target list into normalized ATC values."""
    return tuple(sorted({item.strip() for item in value.split(",") if item.strip()}))


def parse_target_mode(value: str) -> TargetMode:
    """Parse a CLI mode without silently falling back to a broader target."""
    match value:
        case "existing" | "all" | "uncovered" | "explicit":
            return value
        case unsupported:
            raise TargetSelectionError(f"unsupported target mode: {unsupported}")


def scope_id(atc4: str) -> str:
    """Build a stable market-scope id for one ATC4."""
    return f"atc4:{atc4}"
