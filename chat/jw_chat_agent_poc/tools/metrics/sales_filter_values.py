from __future__ import annotations

from typing import Any


def num(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def krw_to_eok(value: Any) -> float | None:
    number = num(value)
    return round(number / 100_000_000, 2) if number is not None else None


def format_krw(value: Any) -> str:
    eok = krw_to_eok(value)
    return "N/A" if eok is None else f"{eok:,.2f}억원"


def format_pct(value: Any) -> str:
    return f"{float(value):.2f}%" if isinstance(value, int | float) else "N/A"
