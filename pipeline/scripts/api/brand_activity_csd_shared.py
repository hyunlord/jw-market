from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
import json
from typing import Any, Final, TypeAlias

from pipeline.scripts.analysis.brand_activity.alias.normalize import normalize_iqvia_en


JsonMap: TypeAlias = dict[str, Any]

RX_MEASURES: Final[tuple[str, ...]] = ("sales", "unit", "counting_unit", "dosage_unit")
PUBLIC_MEASURES: Final[tuple[str, ...]] = ("activity", *RX_MEASURES)
SOURCE: Final = "iqvia_nsa"
RANKING_MEASURE: Final = "sales"


class CsdTimeseriesInputError(RuntimeError):
    """Raised when a CSD timeseries request cannot be parsed."""


class CsdMarketFilterError(RuntimeError):
    """Raised when a requested CSD market is not available for the brand."""

    def __init__(self, requested: str, *, available: Sequence[str]) -> None:
        super().__init__(f"unsupported csd_market: {requested}")
        self.requested = requested
        self.available = tuple(available)


class CsdTimeseriesNoMappingError(RuntimeError):
    """Raised when the selected brand has no product in any CSD market."""

    csd_source_present: Final = False


class CsdTimeseriesAmbiguousMarketError(RuntimeError):
    """Raised when mart products do not identify exactly one CSD market."""

    def __init__(self, message: str, *, candidates: Sequence[JsonMap] = ()) -> None:
        super().__init__(message)
        self.candidates = tuple(dict(item) for item in candidates)


@dataclass(frozen=True, slots=True)
class ViewConfig:
    """Table and column names for one mart view family."""

    brand_table: str
    market_table: str
    market_key: str
    market_name_column: str
    ranking_column: str
    has_is_jw: bool


@dataclass(frozen=True, slots=True)
class BrandChoice:
    """One selected or competitor brand returned by the endpoint."""

    brand_key: str
    brand_name: str
    sales_rank: int | None
    is_selected: bool


@dataclass(frozen=True, slots=True)
class BrandMeta:
    """Resolved mart brand metadata used to join IQVIA and CSD."""

    brand_key: str
    brand_name: str
    product_codes: tuple[str, ...]
    is_jw: bool


@dataclass(frozen=True, slots=True)
class CsdCrosswalk:
    """Resolved CSD market with overlap evidence."""

    market: str
    display_market: str
    overlap: tuple[str, ...]
    score: int


def period_ym_to_quarter(period_ym: str) -> str:
    """Convert ``YYYY-MM`` to ``YYYY-QN``."""

    year, month_text = period_ym.split("-", 1)
    month = int(month_text)
    quarter = ((month - 1) // 3) + 1
    return f"{year}-Q{quarter}"


def full_quarters_from_months(months: list[str]) -> list[str]:
    """Return quarters whose three calendar months are present."""

    seen: dict[str, set[int]] = {}
    for period in months:
        quarter = period_ym_to_quarter(period)
        seen.setdefault(quarter, set()).add(int(period.split("-", 1)[1]))
    return sorted(quarter for quarter, present in seen.items() if len(present) == 3)


def months_in_quarter_window(all_months: Sequence[str], quarters: Sequence[str]) -> tuple[str, ...]:
    """Return period_ym values whose quarter belongs to the requested quarter window."""

    quarter_set = set(quarters)
    return tuple(sorted(month for month in all_months if period_ym_to_quarter(month) in quarter_set))


def select_ranked_brands(ranking: list[JsonMap], *, selected_brand: str) -> list[BrandChoice]:
    """Return selected brand plus top competitors, filling to six when possible."""

    by_key = {text(item.get("brand_key")): item for item in ranking}
    selected_item = by_key.get(selected_brand, {"brand_key": selected_brand, "brand": selected_brand, "rank": None})
    selected = _choice(selected_item, is_selected=True)
    choices = [selected]
    used = {selected.brand_key}
    for item in ranking:
        brand_key = text(item.get("brand_key"))
        if not brand_key or brand_key in used:
            continue
        choices.append(_choice(item, is_selected=False))
        used.add(brand_key)
        if len(choices) == 6:
            break
    return choices


def normalized_product_overlap(left: set[str], right: set[str]) -> set[str]:
    """Return normalized product codes shared by two sources."""

    return {normalize_iqvia_en(value) for value in left} & {normalize_iqvia_en(value) for value in right}


def json_map(value: Any) -> JsonMap:
    """Parse a JSON object-like value from DB JSON columns."""

    if isinstance(value, dict):
        return value
    if isinstance(value, str | bytes | bytearray):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def display_csd_market(value: str) -> str:
    """Return the CSD sheet-style market name without the suffix."""

    return value.removesuffix(" Market").removesuffix(" market")


def text(value: Any) -> str:
    """Return a string value, or an empty string for non-text."""

    return value if isinstance(value, str) else ""


def float_value(value: Any) -> float:
    """Return a numeric value as float."""

    return float(value) if isinstance(value, int | float | Decimal) else 0.0


def ratio(value: float, total: float) -> float:
    """Return a percentage ratio with zero-total protection."""

    return (value / total * 100.0) if total else 0.0


def int_or_none(value: Any) -> int | None:
    """Return an integer for rank-like DB values."""

    return int(value) if isinstance(value, int | float) else None


def first(values: tuple[str, ...]) -> str:
    """Return the first product code, or empty string."""

    return values[0] if values else ""


def _choice(item: JsonMap, *, is_selected: bool) -> BrandChoice:
    return BrandChoice(text(item.get("brand_key")), text(item.get("brand") or item.get("brand_key")), int_or_none(item.get("rank")), is_selected)
