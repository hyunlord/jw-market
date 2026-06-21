from __future__ import annotations

from typing import Any, Final, Mapping


INTEREST_LEVELS: Final[tuple[str, ...]] = ("VERY USEFUL", "SOMEWHAT USEFUL", "NOT AT ALL")
RX_FREQ_LEVELS: Final[tuple[str, ...]] = (
    "frequently",
    "occasionally",
    "lapsed user",
    "never",
    "new to me, thus never prescribed",
)
RX_EVOLUTION_LEVELS: Final[tuple[str, ...]] = (
    "increase (or will begin to prescribe)",
    "remain unchanged",
    "decrease",
)

INTEREST_DEFAULT_WEIGHTS: Final[Mapping[str, float]] = {
    "VERY USEFUL": 1.0,
    "SOMEWHAT USEFUL": 0.5,
    "NOT AT ALL": 0.0,
}
RX_FREQ_DEFAULT_WEIGHTS: Final[Mapping[str, float]] = {
    "frequently": 1.0,
    "occasionally": 0.6,
    "lapsed user": 0.3,
    "never": 0.0,
    "new to me, thus never prescribed": 0.0,
}
RX_EVOLUTION_DEFAULT_WEIGHTS: Final[Mapping[str, float]] = {
    "increase (or will begin to prescribe)": 1.0,
    "remain unchanged": 0.5,
    "decrease": 0.0,
}

CSD_TOTAL_CHANNEL: Final = "TOTAL"


def resolved_weights(overrides: Mapping[str, Any] | None) -> dict[str, dict[str, float]]:
    """Return default score weights with valid request overrides applied."""

    raw = overrides or {}
    return {
        "interest": _axis_weights(INTEREST_DEFAULT_WEIGHTS, raw.get("interest")),
        "rx_frequency": _axis_weights(RX_FREQ_DEFAULT_WEIGHTS, raw.get("rx_frequency")),
        "prescription_evolution": _axis_weights(RX_EVOLUTION_DEFAULT_WEIGHTS, raw.get("prescription_evolution")),
    }


def levels_payload() -> dict[str, list[str]]:
    """Return the supported ordered levels for every matrix axis."""

    return {
        "interest": list(INTEREST_LEVELS),
        "rx_frequency": list(RX_FREQ_LEVELS),
        "prescription_evolution": list(RX_EVOLUTION_LEVELS),
    }


def _axis_weights(defaults: Mapping[str, float], overrides: Any) -> dict[str, float]:
    merged = dict(defaults)
    if isinstance(overrides, dict):
        for level, value in overrides.items():
            if level in merged and isinstance(value, int | float):
                merged[str(level)] = float(value)
    return merged
