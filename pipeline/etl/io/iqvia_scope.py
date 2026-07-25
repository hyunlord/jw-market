from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import Any


_QUARTER_PATTERN = re.compile(r"^(\d{4})-?Q([1-4])$", flags=re.IGNORECASE)


def normalize_iqvia_quarters(values: Iterable[str] | None) -> tuple[str, ...]:
    if values is None:
        return ()
    normalized: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        match = _QUARTER_PATTERN.fullmatch(text)
        if not match:
            raise ValueError(f"invalid IQVIA quarter: {value!r}")
        normalized.add(f"{match.group(1)}-Q{match.group(2)}")
    return tuple(sorted(normalized))


def normalize_iqvia_atc4_codes(values: Iterable[str] | None) -> tuple[str, ...]:
    if values is None:
        return ()
    return tuple(
        sorted(
            {
                str(value).strip().upper()
                for value in values
                if str(value or "").strip()
            }
        )
    )


def iqvia_record_in_scope(
    record: dict[str, Any],
    *,
    quarters: tuple[str, ...],
    atc4_codes: tuple[str, ...],
) -> bool:
    if quarters:
        period = str(record.get("period_label") or "").strip()
        match = _QUARTER_PATTERN.fullmatch(period)
        canonical = f"{match.group(1)}-Q{match.group(2)}" if match else period
        if canonical not in quarters:
            return False
    if atc4_codes:
        payload = json.loads(str(record.get("payload") or "{}"))
        static = payload.get("static") or {}
        atc4_code = str(static.get("ATC 4 CODE") or "UNKNOWN").strip().upper()
        if atc4_code not in atc4_codes:
            return False
    return True
