"""Canonical ATC4 identity shared by serving and mart generation."""

from __future__ import annotations

import re
from typing import Final


_ATC4_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^([A-Za-z])(\d+)([A-Za-z])(\d*)$"
)


def normalize_atc4(code: str | None) -> str:
    """Return the five-position identity used to compare ATC4 spellings."""

    raw = str(code or "").strip().upper()
    match = _ATC4_PATTERN.fullmatch(raw)
    if match is None:
        return raw
    lead, first_digits, middle, trailing_digits = match.groups()
    return f"{lead}{int(first_digits):02d}{middle}{int(trailing_digits or 0):01d}"


def atc4_source_aliases(code: str | None) -> tuple[str, ...]:
    """Return canonical and shortened source spellings in stable order."""

    canonical = normalize_atc4(code)
    if _ATC4_PATTERN.fullmatch(canonical) is None or len(canonical) != 5:
        return (canonical,) if canonical else ()
    candidates = [canonical]
    if canonical[1] == "0":
        candidates.append(canonical[0] + canonical[2:])
    if canonical.endswith("0"):
        candidates.append(canonical[:-1])
    if canonical[1] == "0" and canonical.endswith("0"):
        candidates.append(canonical[0] + canonical[2:-1])
    return tuple(dict.fromkeys(candidates))
