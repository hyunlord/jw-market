from __future__ import annotations

CSD_MARKET_ATC4_ORDER = (
    "A02B2",
    "C10C0",
    "C10A1",
    "A10N1",
    "G04C2",
    "K01D2",
    "C11A1",
    "B03A1",
    "A03F0",
    "A10N3",
    "K01A3",
    "V03G2",
    "V06D0",
)


def expected_markets() -> tuple[str, ...]:
    """Return CSD-backed ATC4 values that collapse to 11 final markets."""
    return CSD_MARKET_ATC4_ORDER


def scope_id(atc4: str) -> str:
    """Build a stable market-scope id for one ATC4."""
    return f"atc4:{atc4}"
