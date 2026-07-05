from __future__ import annotations

import math


def display_number(value: float | None) -> str | None:
    if value is None:
        return None
    if abs(value) >= 100_000_000:
        return f"{value / 100_000_000:.1f}억원"
    if abs(value) >= 10_000:
        return f"{value:,.0f}원"
    return f"{value:.2f}".rstrip("0").rstrip(".")


def display_pct(value: float | None) -> str | None:
    if value is None:
        return None
    if value == 0:
        return "0%"
    if abs(value) < 0.1:
        return f"{_format_small_percent(value)}%"
    return f"{value:.1f}%"


def display_aliases(key: str, value: float | None) -> list[str]:
    if value is None:
        return []
    if key.endswith("_pct") or key == "contribution_pct":
        display = display_pct(value)
        return [display] if display else []
    aliases = {display_number(value)}
    aliases.add(f"{value:,.0f}원")
    if abs(value) >= 10_000:
        aliases.add(f"{value / 10_000:,.0f}만원")
        truncated_manwon = math.trunc(value / 10_000)
        aliases.add(f"{truncated_manwon:,.0f}만원")
    if abs(value) >= 100_000_000:
        aliases.add(f"{value / 100_000_000:.1f}억원")
    return sorted(alias for alias in aliases if alias)


def _format_small_percent(value: float) -> str:
    # Preserve two significant digits for tiny percentages; one-decimal
    # formatting turns 0.05% signals into 0.0%, which wf316 cannot quote
    # without failing display-number validation.
    magnitude = abs(value)
    decimals = max(2, min(6, 1 - math.floor(math.log10(magnitude))))
    return f"{value:.{decimals}f}".rstrip("0").rstrip(".")
