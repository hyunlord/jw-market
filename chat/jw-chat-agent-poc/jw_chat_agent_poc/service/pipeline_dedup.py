from __future__ import annotations

from collections.abc import Callable


def memoize_exact_input(transform: Callable[[str], str]) -> Callable[[str], str]:
    cache: dict[str, str] = {}

    def apply(value: str) -> str:
        if value in cache:
            return cache[value]
        result = transform(value)
        cache[value] = result
        return result

    return apply
