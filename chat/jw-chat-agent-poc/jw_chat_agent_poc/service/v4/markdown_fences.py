from __future__ import annotations

import re


FenceState = tuple[str, int]

_FENCE_OPEN_RE = re.compile(r"^(`{3,}|~{3,})")


def advance_fence_state(
    current: FenceState | None,
    line: str,
) -> tuple[FenceState | None, bool]:
    """Return the next fence state and whether the line is a fence boundary."""
    stripped = _strip_blockquote_prefix(line)
    if current is None:
        match = _FENCE_OPEN_RE.match(stripped)
        if match is None:
            return None, False
        marker = match.group(1)
        return (marker[0], len(marker)), True

    character, minimum_length = current
    if (
        len(stripped) >= minimum_length
        and stripped
        and all(value == character for value in stripped)
    ):
        return None, True
    return current, False


def _strip_blockquote_prefix(line: str) -> str:
    stripped = line.strip()
    while stripped.startswith(">"):
        stripped = stripped[1:].lstrip()
    return stripped
