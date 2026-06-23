from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq


PROJECT_ROOT = Path(__file__).resolve().parents[3]
ML_MARKET_PATHS = (
    PROJECT_ROOT / "output" / "catalog" / "ml_market" / "ml_market.parquet",
    PROJECT_ROOT / "parquet" / "ml_market" / "ml_market.parquet",
)
CD_MARKET_PATHS = (
    PROJECT_ROOT / "output" / "catalog" / "cd_market" / "cd_market.parquet",
    PROJECT_ROOT / "parquet" / "cd_market" / "cd_market.parquet",
)
STRATEGIC_BRAND_PATHS = (
    PROJECT_ROOT / "output" / "catalog" / "strategic_brand" / "strategic_brand.parquet",
    PROJECT_ROOT / "parquet" / "strategic_brand" / "strategic_brand.parquet",
)
CD_DIM_PATHS = (
    PROJECT_ROOT / "output" / "catalog" / "dim_market_competitive_dynamics" / "dim_market_competitive_dynamics.parquet",
    PROJECT_ROOT / "parquet" / "dim_market_competitive_dynamics" / "dim_market_competitive_dynamics.parquet",
)


@dataclass(frozen=True, slots=True)
class MarketDefinitionDisplay:
    label: str
    full: str
    atc_codes: list[str]
    atc_count: int
    cd_definition_class: str | None = None


def market_definition_label(atc_codes: list[str]) -> str:
    return "1 ATC" if len(atc_codes) == 1 else f"{len(atc_codes)} ATC 통합"


def ml_atc_codes(ml_id: str) -> list[str]:
    return list(_ml_atc_codes().get(ml_id, []))


def ml_display_for_id(ml_id: str, market_name: str | None = None) -> MarketDefinitionDisplay:
    atc_codes = ml_atc_codes(ml_id)
    full = f"{market_name} 경쟁 시장 ({', '.join(atc_codes)})" if market_name else ", ".join(atc_codes)
    return MarketDefinitionDisplay(
        label=market_definition_label(atc_codes),
        full=full,
        atc_codes=atc_codes,
        atc_count=len(atc_codes),
    )


def cd_display_for_id(cd_id: str | None) -> MarketDefinitionDisplay | None:
    if not cd_id:
        return None
    row = _cd_dim_by_id().get(str(cd_id))
    if row is None:
        return None
    label = _display_label(row)
    if not label:
        return None
    full = _valid_text(row.get("cd_filter_expression")) or label
    return MarketDefinitionDisplay(
        label=label,
        full=full,
        atc_codes=[label],
        atc_count=1,
        cd_definition_class=label,
    )


def cd_display_for_catalog_row(catalog_row: dict[str, Any] | None) -> MarketDefinitionDisplay | None:
    if not catalog_row:
        return None
    cd_id = _valid_text(catalog_row.get("cd_id")) or _valid_text(catalog_row.get("cd_market_id"))
    return cd_display_for_id(cd_id)


def cd_display_for_brand(brand_name: str, ml_id: str | None = None) -> MarketDefinitionDisplay | None:
    cd_id = _cd_id_for_brand(brand_name, ml_id)
    return cd_display_for_id(cd_id)


def _display_label(row: dict[str, Any]) -> str | None:
    return _valid_text(row.get("cd_definition_brand_class")) or _first_filter_token(row.get("cd_filter_raw_json"))


def _first_filter_token(raw_json: Any) -> str | None:
    raw_rows = _loads_json_maybe(raw_json)
    if not isinstance(raw_rows, list):
        return None
    for row in raw_rows:
        if not isinstance(row, dict):
            continue
        value = _valid_text(row.get("value"))
        if value:
            return value
    return None


def _cd_id_for_brand(brand_name: str, ml_id: str | None) -> str | None:
    normalized = _normalize_brand(brand_name)
    for row in _strategic_brand_rows():
        if ml_id and _valid_text(row.get("ml_id")) != ml_id:
            continue
        row_name = _valid_text(row.get("canonical_name")) or _valid_text(row.get("name"))
        if row_name and _normalize_brand(row_name) == normalized:
            return _valid_text(row.get("cd_id"))
    return None


def _normalize_brand(value: str) -> str:
    return value.replace(" ", "").lower()


def _valid_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return None
    return text


@lru_cache(maxsize=1)
def _ml_atc_codes() -> dict[str, list[str]]:
    rows = _read_first_table(ML_MARKET_PATHS, ["ml_id", "atc_codes_json"])
    return {
        str(row["ml_id"]): _parse_list(row.get("atc_codes_json"))
        for row in rows
        if row.get("ml_id")
    }


@lru_cache(maxsize=1)
def _cd_dim_by_id() -> dict[str, dict[str, Any]]:
    rows = _read_first_table(
        CD_DIM_PATHS,
        [
            "competitive_dynamics_id",
            "cd_definition_brand_class",
            "cd_filter_expression",
            "cd_filter_raw_json",
        ],
    )
    return {
        str(row["competitive_dynamics_id"]): row
        for row in rows
        if row.get("competitive_dynamics_id")
    }


@lru_cache(maxsize=1)
def _strategic_brand_rows() -> tuple[dict[str, Any], ...]:
    rows = _read_first_table(STRATEGIC_BRAND_PATHS, ["name", "canonical_name", "ml_id", "cd_id", "is_jw"])
    return tuple(row for row in rows if bool(row.get("is_jw")))


def _read_first_table(paths: tuple[Path, ...], columns: list[str]) -> list[dict[str, Any]]:
    for path in paths:
        if not path.exists():
            continue
        return pq.read_table(path, columns=columns).to_pylist()
    return []


def _parse_list(raw_value: Any) -> list[str]:
    raw_codes = _loads_json_maybe(raw_value)
    if not isinstance(raw_codes, list):
        return []
    return [str(code).strip() for code in raw_codes if str(code).strip()]


def _loads_json_maybe(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value
