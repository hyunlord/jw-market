"""Filter option list helpers for dynamic market UIs."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
import re

from pipeline.scripts.api import db
from pipeline.scripts.api.dynamic_market.resolvers import normalize_source
from pipeline.scripts.api.dynamic_market.types import DynamicMarketInputError, quote_identifier


GENERAL_DIMENSION_TABLE = "mart_general_filter_dimension_metric"
STRATEGIC_DIMENSION_TABLE = "mart_strategic_filter_dimension_metric"
SELECTABLE_ATC_LEVELS = ("atc3", "atc4")
ATC_FIVE_STYLE_RE = re.compile(r"^[A-Z][0-9]{2}[A-Z][0-9]$")
ATC_UBIST_FOUR_STYLE_RE = re.compile(r"^[A-Z][0-9]{2}[A-Z]$")
ATC_UBIST_SHORT_FOUR_STYLE_RE = re.compile(r"^[A-Z][0-9][A-Z][0-9]$")
ATC_UBIST_THREE_STYLE_RE = re.compile(r"^[A-Z][0-9][A-Z]$")
DIMENSION_LABELS: dict[str, str] = {
    "seller": "판매사",
    "molecule_strength": "성분용량",
    "form": "제형",
    "route": "투여경로",
    "reimbursement": "급여구분",
    "mfr": "MFR NAME KOR",
    "molecule_type": "MOLECULE TYPE",
    "strength": "STRENGTH",
    "nhi": "NHI TYPE",
}
DIMENSION_ORDER_BY_SOURCE: dict[str, tuple[str, ...]] = {
    "ubist": ("seller", "molecule_strength", "form", "route", "reimbursement"),
    "iqvia_nsa": ("mfr", "molecule_type", "strength", "nhi"),
}


@dataclass(frozen=True, slots=True)
class DimensionOptionRow:
    dimension_type: str
    dimension_value: str
    dimension_value_norm: str
    row_count: int


def build_filter_options(
    *,
    mart_db: str,
    view: str,
    source: str,
    market_id: str | None = None,
    general_dimension_db: str | None = None,
    strategic_dimension_db: str | None = None,
) -> dict[str, object]:
    normalized_view = normalize_view(view)
    normalized_source = normalize_source(source)
    dimension_db = (general_dimension_db if normalized_view == "general" else strategic_dimension_db) or mart_db
    dimensions = _load_dimension_options(
        dimension_db=dimension_db,
        view=normalized_view,
        source=normalized_source,
        market_id=market_id,
    )
    atc_rows = _load_atc_rows(mart_db=mart_db, view=normalized_view, source=normalized_source, market_id=market_id)
    return build_filter_option_payload(
        view=normalized_view,
        source=normalized_source,
        market_id=market_id,
        dimensions=dimensions,
        atc_rows=atc_rows,
    )


def build_brand_option_check(
    *,
    mart_db: str,
    brand: str,
    view: str,
    source: str,
    market_id: str | None = None,
    general_dimension_db: str | None = None,
    strategic_dimension_db: str | None = None,
) -> dict[str, object]:
    """Return all option values plus the values already carried by one brand.

    The portal uses this as a short-term test2 convenience endpoint: it can
    draw the same option list as ``filter-options`` and pre-check all
    product-level sidecar dimensions that the selected brand actually owns.
    We deliberately read from the view-specific sidecar so strategic recode
    values never leak back to the general ATC sidecar, and vice versa.
    """

    payload = build_filter_options(
        mart_db=mart_db,
        general_dimension_db=general_dimension_db,
        strategic_dimension_db=strategic_dimension_db,
        view=view,
        source=source,
        market_id=market_id,
    )
    normalized_view = str(payload["view"])
    normalized_source = str(payload["source"])
    dimension_db = (general_dimension_db if normalized_view == "general" else strategic_dimension_db) or mart_db
    return {
        **payload,
        "brand": brand,
        "brand_matched": _load_brand_dimension_matches(
            dimension_db=dimension_db,
            brand=brand,
            view=normalized_view,
            source=normalized_source,
            market_id=market_id,
        ),
    }


def build_filter_option_payload(
    *,
    view: str,
    source: str,
    market_id: str | None,
    dimensions: Sequence[DimensionOptionRow],
    atc_rows: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    grouped: dict[str, list[DimensionOptionRow]] = defaultdict(list)
    for row in dimensions:
        grouped[row.dimension_type].append(row)
    ordered_dimensions: list[dict[str, object]] = []
    for dimension_type in DIMENSION_ORDER_BY_SOURCE.get(source, ()):
        rows = sorted(grouped.get(dimension_type, ()), key=lambda item: item.dimension_value)
        ordered_dimensions.append(
            {
                "dimension_type": dimension_type,
                "label": DIMENSION_LABELS.get(dimension_type, dimension_type),
                "values": [
                    {"key": item.dimension_value_norm, "value": item.dimension_value, "row_count": item.row_count}
                    for item in rows
                ],
            }
        )
    return {
        "view": view,
        "source": source,
        "market_id": market_id,
        "dimensions": ordered_dimensions,
        "atc": build_atc_hierarchy(atc_rows),
    }


def build_atc_hierarchy(rows: Iterable[Mapping[str, object]]) -> dict[str, object]:
    buckets: dict[str, dict[str, str]] = {"atc1": {}, "atc2": {}, "atc3": {}, "atc4": {}}
    for row in rows:
        code = str(row.get("atc4_code") or "").strip().upper()
        parsed = parse_atc_code(code)
        if parsed is None:
            continue
        for level, value in parsed.items():
            if value:
                buckets[level].setdefault(value, value)
    return {
        **{
            level: [
                {"key": value, "value": value, "label": value}
                for value in sorted(values)
            ]
            for level, values in buckets.items()
        },
        "selectable_levels": list(SELECTABLE_ATC_LEVELS),
    }


def parse_atc_code(code: str) -> dict[str, str] | None:
    """Return code-only ATC hierarchy prefixes from the deployed ATC4 code shapes."""

    normalized = code.strip().upper()
    if not normalized:
        return None
    if ATC_FIVE_STYLE_RE.fullmatch(normalized):
        return {"atc1": normalized[:1], "atc2": normalized[:3], "atc3": normalized[:4], "atc4": normalized}
    if ATC_UBIST_FOUR_STYLE_RE.fullmatch(normalized):
        return {"atc1": normalized[:1], "atc2": normalized[:3], "atc3": normalized, "atc4": normalized}
    if ATC_UBIST_SHORT_FOUR_STYLE_RE.fullmatch(normalized):
        return {"atc1": normalized[:1], "atc2": normalized[:2], "atc3": normalized[:3], "atc4": normalized}
    if ATC_UBIST_THREE_STYLE_RE.fullmatch(normalized):
        return {"atc1": normalized[:1], "atc2": normalized[:2], "atc3": normalized, "atc4": normalized}
    return {"atc1": normalized[:1], "atc2": normalized[:3], "atc3": normalized[:4], "atc4": normalized}


def normalize_view(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in {"general", "strategic"}:
        raise DynamicMarketInputError(f"unsupported filter option view: {value}")
    return normalized


def _load_dimension_options(*, dimension_db: str, view: str, source: str, market_id: str | None) -> tuple[DimensionOptionRow, ...]:
    table = GENERAL_DIMENSION_TABLE if view == "general" else STRATEGIC_DIMENSION_TABLE
    where = ["source = %s"]
    params: list[str] = [source]
    if view == "general":
        # General view dimensions are raw ATC-sidecar values, so the option
        # universe intentionally spans the whole source.  The default checked
        # values stay market-scoped in _load_brand_dimension_matches.
        rows = db.fetch_all(
            f"""
            SELECT dimension_type,
                   MIN(dimension_value) AS dimension_value,
                   MIN(dimension_value_norm) AS dimension_value_norm,
                   COUNT(*) AS row_count
            FROM {quote_identifier(dimension_db)}.{table} FORCE INDEX (idx_general_option_universe)
            WHERE source = %s
            GROUP BY dimension_type, dimension_value_hash
            ORDER BY dimension_type, dimension_value_hash
            """,
            params,
        )
        return _dimension_option_rows(rows)
    elif market_id:
        market_kind, normalized_market_id = _strategic_market_filter(market_id)
        where.extend(["market_kind = %s", "market_id = %s"])
        params.extend([market_kind, normalized_market_id])
    rows = db.fetch_all(
        f"""
        SELECT dimension_type, dimension_value, dimension_value_norm, COUNT(*) AS row_count
        FROM {quote_identifier(dimension_db)}.{table}
        WHERE {" AND ".join(where)}
        GROUP BY dimension_type, dimension_value, dimension_value_norm
        ORDER BY dimension_type, dimension_value
        """,
        params,
    )
    return _dimension_option_rows(rows)


def _dimension_option_rows(rows: Sequence[Mapping[str, object]]) -> tuple[DimensionOptionRow, ...]:
    return tuple(
        DimensionOptionRow(
            dimension_type=str(row["dimension_type"]),
            dimension_value=str(row["dimension_value"]),
            dimension_value_norm=str(row["dimension_value_norm"]),
            row_count=int(row["row_count"]),
        )
        for row in rows
    )


def _load_brand_dimension_matches(
    *,
    dimension_db: str,
    brand: str,
    view: str,
    source: str,
    market_id: str | None,
) -> dict[str, list[str]]:
    table = GENERAL_DIMENSION_TABLE if view == "general" else STRATEGIC_DIMENSION_TABLE
    allowed_dimensions = DIMENSION_ORDER_BY_SOURCE.get(source, ())
    if not allowed_dimensions:
        return {}

    where = [
        "source = %s",
        "(brand_name = %s OR brand_key = %s OR LOWER(REPLACE(brand_name, ' ', '')) = LOWER(REPLACE(%s, ' ', '')) OR LOWER(REPLACE(brand_key, ' ', '')) = LOWER(REPLACE(%s, ' ', '')))",
    ]
    params: list[object] = [source, brand, brand, brand, brand]
    if view == "general":
        if atc_prefix := _general_atc_prefix(market_id):
            where.append("atc4_code LIKE %s")
            params.append(atc_prefix)
    elif market_id:
        market_kind, normalized_market_id = _strategic_market_filter(market_id)
        where.extend(["market_kind = %s", "market_id = %s"])
        params.extend([market_kind, normalized_market_id])

    rows = db.fetch_all(
        f"""
        SELECT dimension_type, dimension_value_norm
        FROM {quote_identifier(dimension_db)}.{table}
        WHERE {" AND ".join(where)}
        GROUP BY dimension_type, dimension_value_norm
        ORDER BY dimension_type, dimension_value_norm
        """,
        params,
    )
    grouped: dict[str, list[str]] = {dimension_type: [] for dimension_type in allowed_dimensions}
    for row in rows:
        dimension_type = str(row["dimension_type"])
        value = str(row["dimension_value_norm"])
        if dimension_type in grouped and value:
            grouped[dimension_type].append(value)
    return {dimension_type: values for dimension_type, values in grouped.items() if values}


def _load_atc_rows(*, mart_db: str, view: str, source: str, market_id: str | None) -> tuple[dict[str, object], ...]:
    if view == "strategic":
        table, id_column = _strategic_atc_table(market_id)
        where = ["source = %s"]
        params: list[str] = [source]
        if market_id:
            _, normalized_market_id = _strategic_market_filter(market_id)
            where.append(f"{id_column} = %s")
            params.append(normalized_market_id)
        sql = f"""
            SELECT DISTINCT JSON_UNQUOTE(JSON_EXTRACT(by_dimension, '$.atc4_code')) AS atc4_code
            FROM {quote_identifier(mart_db)}.{table}
            WHERE {" AND ".join(where)}
        """
        return tuple(db.fetch_all(sql, params))
    where = ["source = %s"]
    params: list[str] = [source]
    # General ATC choices follow the same all-source universe as the general
    # dimension options.  Strategic ATC rows remain market-scoped above.
    rows = db.fetch_all(
        f"""
        SELECT atc4_code
        FROM {quote_identifier(mart_db)}.mart_general_brand_metric FORCE INDEX (idx_general_atc_universe)
        WHERE {" AND ".join(where)}
        GROUP BY atc4_code
        ORDER BY atc4_code
        """,
        params,
    )
    return tuple(rows)


def _general_atc_prefix(market_id: str | None) -> str | None:
    if not market_id:
        return None
    normalized = market_id.strip().upper()
    if not normalized:
        return None
    return f"{normalized}%"


def _strategic_market_filter(market_id: str) -> tuple[str, str]:
    normalized = market_id.strip()
    if normalized.startswith("ml_"):
        return "ml", normalized
    if normalized.startswith("cd_"):
        return "cd", normalized
    if normalized.startswith("strategy_"):
        return "ml", f"ml_{normalized.removeprefix('strategy_')}"
    raise DynamicMarketInputError(f"unsupported strategic market id: {market_id}")


def _strategic_atc_table(market_id: str | None) -> tuple[str, str]:
    if market_id and market_id.strip().startswith("cd_"):
        return "mart_strategic_cd_brand_metric", "cd_market_id"
    return "mart_strategic_ml_brand_metric", "ml_id"
