from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
import re
from typing import TypeAlias


_NUMERIC_TEXT = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
CanonicalValue: TypeAlias = None | bool | str | Decimal | tuple[object, ...]
CanonicalArgumentKey: TypeAlias = tuple[
    str,
    tuple[tuple[str, CanonicalValue], ...],
]


def canonical_argument_key(
    tool_name: str,
    arguments: Mapping[str, object],
) -> CanonicalArgumentKey:
    return (
        tool_name,
        tuple(
            (str(key), _canonical_value(value))
            for key, value in sorted(
                arguments.items(),
                key=lambda item: str(item[0]),
            )
        ),
    )


def _canonical_value(value: object) -> CanonicalValue:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, Mapping):
        return tuple(
            (str(key), _canonical_value(item))
            for key, item in sorted(
                value.items(),
                key=lambda pair: str(pair[0]),
            )
        )
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return tuple(_canonical_value(item) for item in value)
    if isinstance(value, Decimal):
        return value.normalize()
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value)).normalize()
    if isinstance(value, str) and _NUMERIC_TEXT.fullmatch(value):
        try:
            return Decimal(value).normalize()
        except InvalidOperation:
            return value
    return str(value)
